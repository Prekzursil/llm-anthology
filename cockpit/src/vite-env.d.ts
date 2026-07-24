/// <reference types="vite/client" />

// Vite's client types provide the ambient module declarations this project relies
// on outside plain TS resolution:
//   * `import url from './x?url'`     -> string asset URL (used for the ELK worker)
//   * `import.meta.env.*`             -> typed Vite env vars
// Keeping this reference here means `tsc` resolves those imports without emitting.
