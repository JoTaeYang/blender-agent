"""Blender adapter (plan §9). Thin bridge between ``bpy``/``bmesh`` and the
pure-Python engine. Importing this package does NOT import ``bpy`` -- the
Blender modules are imported lazily inside functions, so the package can be
imported (and unit tested) outside Blender."""
