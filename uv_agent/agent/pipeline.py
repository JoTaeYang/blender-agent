"""Agent orchestrator: the plan -> generate -> pack -> evaluate -> repair loop
(plan §6, §10, Phase 7).

The LLM (or MockProvider) only chooses *actions*; this module deterministically
executes them through the geometry solver and re-evaluates until the result is
accepted or the iteration budget is exhausted.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from uv_agent.agent.llm import LLMProvider, MockProvider
from uv_agent.agent.schema import validate_agent_output
from uv_agent.geometry.evaluation import Evaluation, evaluate_uv_solution, per_island_metrics
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.packing import pack_islands
from uv_agent.geometry.projection import project_island
from uv_agent.geometry.relaxation import relax_island
from uv_agent.geometry.solution import UVMap, UVSolution
from uv_agent.planner import operations
from uv_agent.planner.island_planner import IslandPlan, PlanConstraints, plan_islands


@dataclass
class IterationRecord:
    iteration: int
    evaluation: Evaluation
    agent_output: dict | None
    island_count: int


@dataclass
class RunResult:
    solution: UVSolution
    evaluation: Evaluation
    plan: IslandPlan
    history: list[IterationRecord] = field(default_factory=list)
    manual_review: bool = False

    def to_dict(self) -> dict:
        return {
            "solution": self.solution.to_dict(),
            "evaluation": self.evaluation.to_dict(),
            "plan": self.plan.to_dict(),
            "manual_review": self.manual_review,
            "history": [
                {
                    "iteration": r.iteration,
                    "evaluation": r.evaluation.to_dict(),
                    "island_count": r.island_count,
                    "agent_output": r.agent_output,
                }
                for r in self.history
            ],
        }


class UVAgentPipeline:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        max_iterations: int = 4,
        stretch_threshold: float = 0.25,
        angle_threshold: float = 30.0,
        split_by_material: bool = True,
    ):
        self.provider = provider or MockProvider(stretch_threshold=stretch_threshold)
        self.max_iterations = max_iterations
        self.stretch_threshold = stretch_threshold
        self.angle_threshold = angle_threshold
        self.split_by_material = split_by_material

    # -- public API --------------------------------------------------------
    def run(
        self,
        mesh: MeshGraph,
        user_intent: str = "",
        *,
        constraints: PlanConstraints | None = None,
        memory: list | None = None,
    ) -> RunResult:
        plan = plan_islands(
            mesh,
            angle_threshold=self.angle_threshold,
            split_by_material=self.split_by_material,
            constraints=constraints,
        )
        relax_flags: set[str] = set()
        history: list[IterationRecord] = []
        manual_review = False

        # Keep the best result ever seen so a bad repair can never make the
        # returned layout worse than what we already had (critical on organic
        # meshes where the heuristics over-correct).
        best: dict | None = None
        prev_score: tuple | None = None

        for it in range(self.max_iterations + 1):
            uvmap = UVMap.for_mesh(mesh)
            for isl in plan.islands:
                if not isl.face_ids:
                    continue
                project_island(mesh, isl.face_ids, uvmap, isl.projection)
                if isl.island_id in relax_flags and isl.projection == "planar":
                    relax_island(mesh, isl.face_ids, uvmap)
            transforms = pack_islands(mesh, plan, uvmap)
            evaluation = evaluate_uv_solution(
                mesh, plan, uvmap, stretch_threshold=self.stretch_threshold
            )
            score = self._score(evaluation)

            record = IterationRecord(
                iteration=it,
                evaluation=evaluation,
                agent_output=None,
                island_count=len([i for i in plan.islands if i.face_ids]),
            )
            history.append(record)

            if best is None or score < best["score"]:
                best = {
                    "score": score,
                    "evaluation": evaluation,
                    "uvmap": uvmap.copy(),
                    "transforms": transforms,
                    "plan": copy.deepcopy(plan),
                }

            if evaluation.status == "accepted":
                break
            # Stop if the last repair round did not improve the score
            # (tuple compare: prefer accepted, then lower cost).
            if prev_score is not None and score >= prev_score:
                break
            if it >= self.max_iterations:
                break

            agent_input = self._build_agent_input(
                mesh, plan, uvmap, evaluation, user_intent, it, memory
            )
            agent_output = self.provider.plan(agent_input)
            if validate_agent_output(agent_output):
                agent_output = {"intent": "invalid", "plan": []}
            record.agent_output = agent_output

            prev_score = score
            plan, relax_flags, manual_review, changed = self._apply_actions(
                mesh, plan, agent_output, relax_flags
            )
            if manual_review or not changed:
                break

        solution = UVSolution.from_uvmap(mesh, best["uvmap"], best["transforms"])
        return RunResult(
            solution=solution,
            evaluation=best["evaluation"],
            plan=best["plan"],
            history=history,
            manual_review=manual_review,
        )

    @staticmethod
    def _score(evaluation) -> tuple[int, float]:
        """Lower is better. Prefer 'accepted', then minimize folds + stretch."""
        accepted = 0 if evaluation.status == "accepted" else 1
        cost = evaluation.overlap_ratio * 4.0 + evaluation.stretch_score
        return (accepted, cost)

    # -- internals ---------------------------------------------------------
    def _build_agent_input(
        self, mesh, plan, uvmap, evaluation, user_intent, iteration, memory
    ) -> dict:
        islands = []
        for isl in plan.islands:
            if not isl.face_ids:
                continue
            m = per_island_metrics(mesh, isl.face_ids, uvmap)
            islands.append(
                {
                    "island_id": isl.island_id,
                    "projection": isl.projection,
                    "face_count": len(isl.face_ids),
                    "protected": isl.protected,
                    "overlap_ratio": m["overlap_ratio"],
                    "stretch_score": m["stretch_score"],
                }
            )
        return {
            "user_message": user_intent,
            "iteration": iteration,
            "mesh_summary": {
                "object_id": mesh.object_id,
                "vertex_count": mesh.vertex_count,
                "face_count": mesh.face_count,
            },
            "evaluation": evaluation.to_dict(),
            "islands": islands,
            "memory": memory or [],
            "success_criteria": {
                "stretch_score_max": self.stretch_threshold,
                "overlap_ratio_max": plan.constraints.max_overlap_ratio,
            },
            "available_tools": [
                "set_island_projection",
                "relax_island",
                "split_island",
                "merge_islands",
                "protect_region",
                "manual_review_required",
            ],
        }

    def _apply_actions(self, mesh, plan, agent_output, relax_flags):
        relax_flags = set(relax_flags)
        manual_review = False
        changed = False
        for step in agent_output.get("plan", []):
            tool = step.get("tool")
            args = step.get("args", {}) or {}
            if tool == "set_island_projection":
                isl = plan.island_by_id(args.get("island_id", ""))
                proj = args.get("projection", "planar")
                if isl is not None and proj in ("planar", "cylindrical") and isl.projection != proj:
                    isl.projection = proj
                    changed = True
            elif tool == "relax_island":
                iid = args.get("island_id") or args.get("target_island")
                if iid and plan.island_by_id(iid) is not None and iid not in relax_flags:
                    relax_flags.add(iid)
                    changed = True
                elif not iid:  # relax everything
                    for isl in plan.islands:
                        relax_flags.add(isl.island_id)
                    changed = True
            elif tool == "split_island":
                faces = self._resolve_faces(mesh, args)
                iid = args.get("island_id") or self._island_for_faces(plan, faces)
                if iid and faces:
                    new_plan = operations.split_island(plan, iid, faces)
                    if len(new_plan.islands) != len(plan.islands):
                        plan = new_plan
                        changed = True
            elif tool == "merge_islands":
                ids = args.get("island_ids")
                new_plan = (
                    operations.merge_islands(plan, ids)
                    if ids
                    else operations.merge_small_islands(plan, min_faces=args.get("min_faces", 1))
                )
                if len(new_plan.islands) != len(plan.islands):
                    plan = new_plan
                    changed = True
            elif tool == "protect_region":
                faces = self._resolve_faces(mesh, args)
                if faces:
                    plan = operations.protect_region(plan, faces)
                    changed = True
            elif tool == "repack_all":
                changed = True  # next loop re-packs anyway
            elif tool == "manual_review_required":
                manual_review = True
            # rotate/scale/translate/pin: recorded as advisory in MVP (packer owns layout)
        return plan, relax_flags, manual_review, changed

    @staticmethod
    def _resolve_faces(mesh, args) -> list[int]:
        if "target_faces" in args or "face_ids" in args:
            return list(args.get("target_faces") or args.get("face_ids") or [])
        region = args.get("target_region") or args.get("region")
        if region:
            return operations.faces_for_region(mesh, region)
        return []

    @staticmethod
    def _island_for_faces(plan, faces) -> str | None:
        if not faces:
            return None
        f2i = plan.face_to_island()
        return f2i.get(faces[0])
