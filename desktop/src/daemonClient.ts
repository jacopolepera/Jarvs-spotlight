import { invoke } from "@tauri-apps/api/core";

export type DaemonConnection = {
  httpUrl: string;
  wsUrl: string;
  token: string;
};

let connectionPromise: Promise<DaemonConnection> | null = null;

export const getDaemonConnection = () => {
  if (!connectionPromise) {
    connectionPromise = invoke<DaemonConnection>("daemon_connection");
  }
  return connectionPromise;
};

export const daemonFetch = async (path: string, init: RequestInit = {}) => {
  const connection = await getDaemonConnection();
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${connection.token}`);
  return fetch(`${connection.httpUrl}${path}`, { ...init, headers });
};

export const createDaemonWebSocket = async () => {
  const connection = await getDaemonConnection();
  return new WebSocket(connection.wsUrl, [
    "jarvs-v1",
    `jarvs-auth.${connection.token}`,
  ]);
};
