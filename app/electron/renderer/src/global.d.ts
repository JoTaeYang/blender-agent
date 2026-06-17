import type { RendererApi } from '@shared/contracts';

declare global {
  interface Window {
    api: RendererApi;
  }
}

export {};
