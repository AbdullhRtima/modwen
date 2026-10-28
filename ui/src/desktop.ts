// The desktop app (Tauri) bridge: present when the UI runs inside Modwen's app window, undefined in a browser.
// Only the commands granted in desktop/src-tauri/capabilities/engine-ui.json can be called.

type Unlisten = () => void;
export type TauriBridge = {
  core: { invoke: <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T> };
  event: { listen: <T>(name: string, fn: (e: { payload: T }) => void) => Promise<Unlisten> };
};

export const desktop = (window as unknown as { __TAURI__?: TauriBridge }).__TAURI__;

export type UpdateInfo = { version: string; notes: string };
