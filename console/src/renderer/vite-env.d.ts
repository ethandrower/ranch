/// <reference types="vite/client" />

declare module '*.module.css' {
  const styles: Record<string, string>;
  export default styles;
}

interface ImportMetaEnv {
  readonly VITE_RANCH_VIEW?: string;
  readonly VITE_RANCH_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
