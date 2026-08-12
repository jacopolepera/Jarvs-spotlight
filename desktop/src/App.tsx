import { useState, useEffect, useRef, useCallback } from "react";
import { Settings, Paperclip, X, Minus, Zap, Cloud, Monitor, Trash2, AlertTriangle, ChevronRight, ChevronDown, Plus, Mic } from "lucide-react";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./App.css";
import { createDaemonWebSocket, daemonFetch } from "./daemonClient";
import { THEME_OPTIONS, getThemeAttribute, normalizeThemeName, type ThemeName } from "./themes";

type Attachment = {
  attachmentId: string;
  displayName: string;
  kind: "image" | "file";
  size: number;
};

type ExecutionPolicy = "always_ask" | "auto_run" | "deny_all";
type CloudWorkspaceEgress = "deny" | "allow";

type ModelConfig = {
  id: string;
  name: string;
  provider?: "cloud" | "local";
  model_string?: string;
  api_base?: string | null;
  api_key_configured?: boolean;
};

type MemoryItem = {
  memory_id: string;
  category: "profile" | "preference" | "episode";
  content: string;
  provenance: string;
  confidence: number;
  expires_at?: string | null;
  version: number;
};

type RoutineItem = {
  routine_id: string;
  version: number;
  name: string;
  description: string;
  manifest: Record<string, unknown>;
  manifest_hash: string;
};

type ChatMessage = {
  id: string;
  role: string;
  content: string;
  attachments?: Attachment[];
  generationId?: string;
  thoughtProcess?: string;
  status?: "streaming" | "done" | "error";
};

type ChatEvent = {
  type: "session" | "started" | "chunk" | "done" | "error" | "approval_required" | "activity";
  session_id?: string;
  generation_id?: string;
  code?: string;
  text?: string;
  channel?: "answer" | "thought";
  chunk_type?: string;
  format?: string;
  approval_id?: string;
  tool_name?: string;
  title?: string;
  arguments?: Record<string, unknown> & { language?: string; code?: string };
  invocation_hash?: string;
  expires_at?: string;
  activity?: {
    event?: string;
    sequence?: number;
    payload?: Record<string, unknown>;
  };
};

type PendingApproval = Required<Pick<ChatEvent, "approval_id" | "session_id" | "generation_id">> & {
  title: string;
  toolName: string;
  language: string;
  code: string;
};

type NewSessionRequest = {
  hideAfter: boolean;
  source: string;
};

type NativeErrorEvent = {
  component: string;
  message: string;
};

type VoiceRecordingRequest = {
  action: "start" | "stop";
};

type DaemonStatus = {
  running: boolean;
  starting: boolean;
  pid?: number;
  shortcutRegistered: boolean;
  voiceShortcutRegistered?: boolean;
};

type WindowMode = "compact" | "compactTall" | "expanded";

const createGenerationId = () => {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
};

const blobToBase64 = async (blob: Blob) => {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  const chunkSize = 32_768;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return window.btoa(binary);
};

const formatActivity = (activity: ChatEvent["activity"]) => {
  const event = activity?.event;
  const payload = activity?.payload || {};
  if (event === "route.selected") {
    const profile = payload.profile ? ` · profile ${String(payload.profile)}` : "";
    const count = typeof payload.tool_catalog_size === "number" ? ` · ${payload.tool_catalog_size} tool` : "";
    return `- Route: ${String(payload.lane || "unknown")}${profile}${count}`;
  }
  if (event === "model.started") {
    const phase = payload.phase ? ` · ${String(payload.phase)}` : "";
    const count = typeof payload.tool_catalog_size === "number" ? ` · ${payload.tool_catalog_size} tool` : "";
    return `- Model started (${String(payload.destination || "provider")}${phase}${count})`;
  }
  if (event === "model.completed") {
    const finish = String(payload.finish_reason || "unknown");
    const calls = typeof payload.tool_call_count === "number" ? ` · ${payload.tool_call_count} tool call` : "";
    const duration = typeof payload.duration_ms === "number" ? ` · ${(payload.duration_ms / 1000).toFixed(1)} s` : "";
    return `- Model response: ${finish}${calls}${duration}`;
  }
  if (event === "model.retry_scheduled") return `- Model recovery: ${String(payload.reason_code || "incomplete response")} (${String(payload.profile || payload.phase || "action")})`;
  if (event === "tool.requested") return `- Tool requested: ${String(payload.tool_name || "tool")}`;
  if (event === "tool.approval_required") return "- Waiting for approval";
  if (event === "tool.started") return `- Running: ${String(payload.tool_name || "tool")}`;
  if (event === "tool.completed") return `- Tool completed: ${String(payload.tool_name || "tool")}`;
  if (event === "tool.denied") return `- Tool denied (${String(payload.reason_code || "policy")})`;
  if (event === "run.completed") return "- Activity completed";
  if (event === "run.failed") return `- Activity failed (${String(payload.reason_code || "error")})`;
  return "";
};

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [isResetting, setIsResetting] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [zoomLevel, setZoomLevel] = useState(1.0);
  const [expandedThoughts, setExpandedThoughts] = useState<Set<string>>(() => new Set());
  const wsRef = useRef<WebSocket | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const generationIdRef = useRef<string | null>(null);
  const isStreamingRef = useRef(false);
  const socketConnectedRef = useRef(false);
  const resetInFlightRef = useRef(false);
  const resetRecoveryRequiredRef = useRef(false);
  const resetRecoveryHideAfterRef = useRef(false);
  const pendingHideAfterResetRef = useRef(false);
  const acceptInitialSessionRef = useRef(true);
  const connectionLostDuringGenerationRef = useRef(false);
  const showSettingsRef = useRef(false);
  const settingsButtonRef = useRef<HTMLButtonElement>(null);
  const settingsPanelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const endOfMessagesRef = useRef<HTMLDivElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const recordingRef = useRef(false);
  const voiceStartPendingRef = useRef(false);
  const voiceStopRequestedRef = useRef(false);
  const voiceTimeoutRef = useRef<number | null>(null);
  const isTranscribingRef = useRef(false);

  // Settings state
  const [showSettings, setShowSettings] = useState(false);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [activeModel, setActiveModel] = useState("");
  const [activeProvider, setActiveProvider] = useState("cloud");
  const [apiKey, setApiKey] = useState("");       // Cloud models only
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false);
  const [serverUrl, setServerUrl] = useState(""); // Local models only
  const [executionPolicy, setExecutionPolicy] = useState<ExecutionPolicy>("always_ask");
  const [cloudWorkspaceEgress, setCloudWorkspaceEgress] = useState<CloudWorkspaceEgress>("deny");

  // User-configurable application settings
  const [theme, setTheme] = useState<ThemeName>("Default");
  const [workspacePath, setWorkspacePath] = useState("");
  const [modelString, setModelString] = useState("");
  const [showAddModel, setShowAddModel] = useState(false);
  const [newModel, setNewModel] = useState({ name: "", provider: "cloud", modelString: "", urlOrKey: "" });
  const [memoryItems, setMemoryItems] = useState<MemoryItem[]>([]);
  const [newMemory, setNewMemory] = useState({ category: "preference", content: "" });
  const [routines, setRoutines] = useState<RoutineItem[]>([]);
  const [routineEditor, setRoutineEditor] = useState("");
  const [editingRoutineId, setEditingRoutineId] = useState<string | null>(null);
  const [personalDataError, setPersonalDataError] = useState<string | null>(null);

  const windowMode: WindowMode = messages.length > 0 || showSettings || pendingApproval !== null || sessionError !== null
    ? "expanded"
    : attachments.length > 0
      ? "compactTall"
      : "compact";

  const setStreaming = useCallback((value: boolean) => {
    isStreamingRef.current = value;
  }, []);

  const releaseVoiceStream = useCallback(() => {
    if (voiceTimeoutRef.current !== null) {
      window.clearTimeout(voiceTimeoutRef.current);
      voiceTimeoutRef.current = null;
    }
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
    mediaStreamRef.current = null;
    mediaRecorderRef.current = null;
    recordingRef.current = false;
    setIsRecording(false);
  }, []);

  const transcribeVoiceBlob = useCallback(async (blob: Blob, mimeType: string) => {
    if (!blob.size) {
      setSessionError("The audio recording is empty. Try holding F4 for longer.");
      return;
    }
    if (blob.size > 15 * 1024 * 1024) {
      setSessionError("The recording exceeds the 15 MiB limit.");
      return;
    }
    isTranscribingRef.current = true;
    setIsTranscribing(true);
    setSessionError(null);
    try {
      const response = await daemonFetch("/voice/transcribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          audio_base64: await blobToBase64(blob),
          mime_type: mimeType || "audio/webm",
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || typeof data.text !== "string") {
        throw new Error(data.detail?.message || "Transcription failed.");
      }
      const transcription = data.text.trim();
      if (transcription) {
        setInput((previous) => previous.trim() ? `${previous.trim()} ${transcription}` : transcription);
        window.setTimeout(() => inputRef.current?.focus(), 0);
      }
    } catch (error) {
      console.error("Voice transcription failed", error);
      setSessionError(error instanceof Error ? error.message : "Transcription failed.");
    } finally {
      isTranscribingRef.current = false;
      setIsTranscribing(false);
    }
  }, []);

  const stopVoiceRecording = useCallback(() => {
    voiceStopRequestedRef.current = true;
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state === "recording") {
      recorder.stop();
    }
  }, []);

  const startVoiceRecording = useCallback(async () => {
    if (
      recordingRef.current ||
      voiceStartPendingRef.current ||
      isTranscribingRef.current ||
      isStreamingRef.current ||
      resetInFlightRef.current
    ) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setSessionError("Voice recording is not supported by this WebView.");
      return;
    }
    voiceStartPendingRef.current = true;
    voiceStopRequestedRef.current = false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaStreamRef.current = stream;
      const preferredType = "audio/webm;codecs=opus";
      const options = MediaRecorder.isTypeSupported(preferredType)
        ? { mimeType: preferredType }
        : undefined;
      const recorder = new MediaRecorder(stream, options);
      audioChunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) audioChunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        const chunks = audioChunksRef.current;
        audioChunksRef.current = [];
        const mimeType = recorder.mimeType || chunks[0]?.type || "audio/webm";
        const blob = new Blob(chunks, { type: mimeType });
        releaseVoiceStream();
        void transcribeVoiceBlob(blob, mimeType);
      };
      recorder.onerror = () => {
        releaseVoiceStream();
        setSessionError("Microphone recording was interrupted.");
      };
      mediaRecorderRef.current = recorder;
      recordingRef.current = true;
      setIsRecording(true);
      setSessionError(null);
      recorder.start(250);
      voiceTimeoutRef.current = window.setTimeout(() => {
        stopVoiceRecording();
      }, 60_000);
      if (voiceStopRequestedRef.current) recorder.stop();
    } catch (error) {
      console.error("Microphone access failed", error);
      releaseVoiceStream();
      setSessionError("Microphone access was denied or is unavailable.");
    } finally {
      voiceStartPendingRef.current = false;
    }
  }, [releaseVoiceStream, stopVoiceRecording, transcribeVoiceBlob]);

  const clearConversationUi = useCallback(() => {
    setMessages([]);
    setInput("");
    setAttachments([]);
    setPendingApproval(null);
    setApprovalSubmitting(false);
    setStreaming(false);
    setExpandedThoughts(new Set());
    setShowSettings(false);
    setShowAddModel(false);
    setNewModel({ name: "", provider: "cloud", modelString: "", urlOrKey: "" });
  }, [setStreaming]);

  const startNewSession = useCallback(async ({
    hideAfter = false,
    confirmIfStreaming = true,
  }: {
    hideAfter?: boolean;
    confirmIfStreaming?: boolean;
  } = {}) => {
    if (resetInFlightRef.current) {
      // The latest window intent wins: reopening Jarvs while a close-triggered
      // reset is still running must keep it visible when the reset completes.
      pendingHideAfterResetRef.current = hideAfter;
      resetRecoveryHideAfterRef.current = hideAfter;
      return false;
    }

    if (
      confirmIfStreaming &&
      isStreamingRef.current &&
      !window.confirm("Jarvs Spotlight is still generating a response. The new chat will wait for the current run to finish; a command that has already started may not be interruptible. Continue?")
    ) {
      return false;
    }

    pendingHideAfterResetRef.current = hideAfter;
    resetInFlightRef.current = true;
    acceptInitialSessionRef.current = false;
    setIsResetting(true);
    setSessionError(null);

    // Invalidate the current stream before the HTTP request. Any frames from the
    // previous session/generation are ignored while reset is in flight.
    sessionIdRef.current = null;
    generationIdRef.current = null;
    setSessionId(null);
    clearConversationUi();

    const abortController = new AbortController();
    const resetTimeout = window.setTimeout(() => abortController.abort(), 15_000);

    try {
      const response = await daemonFetch("/session/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: abortController.signal,
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const data = await response.json();
      if (data.status !== "ok" || typeof data.session_id !== "string" || !data.session_id) {
        throw new Error("invalid reset response");
      }
      if (!socketConnectedRef.current || wsRef.current?.readyState !== WebSocket.OPEN) {
        throw new Error("chat socket disconnected");
      }

      acceptInitialSessionRef.current = false;
      resetRecoveryRequiredRef.current = false;
      resetRecoveryHideAfterRef.current = false;
      sessionIdRef.current = data.session_id;
      setSessionId(data.session_id);
      if (pendingHideAfterResetRef.current) {
        try {
          await getCurrentWindow().hide();
        } catch (error) {
          console.error("Failed to hide Jarvs", error);
          setSessionError("The new chat is ready, but the window could not be hidden.");
          return false;
        }
      } else {
        window.setTimeout(() => inputRef.current?.focus(), 0);
      }
      return true;
    } catch (error) {
      console.error("Failed to start a new session", error);
      // Force a fresh session handshake. The backend may have completed the
      // reset even if the HTTP response or socket readiness check failed.
      resetRecoveryRequiredRef.current = true;
      resetRecoveryHideAfterRef.current ||= pendingHideAfterResetRef.current;
      acceptInitialSessionRef.current = true;
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.close();
      }
      setSessionError(
        "A new chat could not be started. A command that has already started may still be running; try again when the service is available."
      );
      return false;
    } finally {
      window.clearTimeout(resetTimeout);
      resetInFlightRef.current = false;
      pendingHideAfterResetRef.current = false;
      setIsResetting(false);
    }
  }, [clearConversationUi]);

  // Disable right-click context menu
  useEffect(() => {
    const handler = (e: MouseEvent) => e.preventDefault();
    document.addEventListener("contextmenu", handler);
    return () => document.removeEventListener("contextmenu", handler);
  }, []);

  // Zoom with Ctrl+Scroll
  useEffect(() => {
    const handleWheel = (e: WheelEvent) => {
      if (e.ctrlKey) {
        e.preventDefault();
        setZoomLevel(prev => {
          const delta = e.deltaY > 0 ? -0.05 : 0.05;
          return Math.min(1.6, Math.max(0.6, Math.round((prev + delta) * 100) / 100));
        });
      }
    };
    window.addEventListener("wheel", handleWheel, { passive: false });
    return () => window.removeEventListener("wheel", handleWheel);
  }, []);

  useEffect(() => {
    endOfMessagesRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    showSettingsRef.current = showSettings;
  }, [showSettings]);

  useEffect(() => {
    document.documentElement.dataset.theme = getThemeAttribute(theme);
  }, [theme]);

  useEffect(() => {
    if (!isTauri()) return;
    invoke("set_main_window_mode", { mode: windowMode }).catch((error) => {
      console.error(`Failed to switch Jarvs to ${windowMode} window mode`, error);
    });
  }, [windowMode]);

  useEffect(() => {
    if (!showSettings) return;

    const panel = settingsPanelRef.current;
    const getFocusable = () => Array.from(
      panel?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      ) ?? []
    );
    window.setTimeout(() => getFocusable()[0]?.focus(), 0);

    const handleDialogKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setShowSettings(false);
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = getFocusable();
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleDialogKeyDown);
    return () => {
      document.removeEventListener("keydown", handleDialogKeyDown);
      window.setTimeout(() => settingsButtonRef.current?.focus(), 0);
    };
  }, [showSettings]);

  const loadSettings = async () => {
    try {
      const res = await daemonFetch("/settings");
      if (res.ok) {
        const data = await res.json();
        setModels(data.models || []);
        setActiveModel(data.active_model || "");
        setTheme(normalizeThemeName(data.theme));
        setWorkspacePath(data.workspace_path || "");
        setExecutionPolicy(
          ["always_ask", "auto_run", "deny_all"].includes(data.execution_policy)
            ? data.execution_policy
            : "always_ask"
        );
        setCloudWorkspaceEgress(data.cloud_workspace_egress === "allow" ? "allow" : "deny");
        // Populate fields from the active model config
        const config = data.active_model_config;
        if (config) {
          const provider = config.provider || "cloud";
          setActiveProvider(provider);
          setModelString(config.model_string || "");
          if (provider === "local") {
            setServerUrl(config.api_base || "http://localhost:1234/v1");
            setApiKey("");
            setApiKeyConfigured(false);
          } else {
            setApiKey("");
            setApiKeyConfigured(config.api_key_configured === true);
            setServerUrl("");
          }
        }
      }
    } catch (e) {
      console.error("Failed to load settings", e);
    }
  };

  useEffect(() => {
    void loadSettings();

    let disposed = false;
    let reconnectTimer: number | undefined;
    let retryAttempt = 0;

    const connect = async () => {
      if (disposed) return;

      let ws: WebSocket;
      try {
        ws = await createDaemonWebSocket();
      } catch (error) {
        if (disposed) return;
        console.error("Failed to read daemon connection", error);
        setSessionError("Could not initialize the secure connection to Jarvs Spotlight.");
        reconnectTimer = window.setTimeout(() => void connect(), 1_000);
        return;
      }
      if (disposed) {
        ws.close();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        if (disposed || wsRef.current !== ws) {
          ws.close();
          return;
        }
        retryAttempt = 0;
        socketConnectedRef.current = true;
        console.log("Connected to Python daemon");
        void loadSettings();
      };

      ws.onmessage = (event) => {
        if (disposed || wsRef.current !== ws) return;

        let data: ChatEvent;
        try {
          data = JSON.parse(event.data) as ChatEvent;
        } catch (error) {
          console.error("Invalid message from Python daemon", error);
          return;
        }

        if (data.type === "session") {
          if (
            !resetInFlightRef.current &&
            acceptInitialSessionRef.current &&
            typeof data.session_id === "string" &&
            data.session_id
          ) {
            if (resetRecoveryRequiredRef.current) {
              const hideAfterRecovery = resetRecoveryHideAfterRef.current;
              resetRecoveryRequiredRef.current = false;
              sessionIdRef.current = data.session_id;
              setSessionId(data.session_id);
              void startNewSession({ hideAfter: hideAfterRecovery });
              return;
            }

            if (connectionLostDuringGenerationRef.current) {
              connectionLostDuringGenerationRef.current = false;
              void startNewSession();
              return;
            }

            const previousSessionId = sessionIdRef.current;
            acceptInitialSessionRef.current = false;
            sessionIdRef.current = data.session_id;
            generationIdRef.current = null;
            setSessionId(data.session_id);
            if (previousSessionId && previousSessionId !== data.session_id) {
              clearConversationUi();
              setSessionError("The service restarted. A new chat is ready.");
            } else {
              setSessionError(null);
            }
          }
          return;
        }

        if (
          data.type === "error" &&
          data.code === "stale_session" &&
          typeof data.session_id === "string" &&
          typeof data.generation_id === "string" &&
          data.generation_id === generationIdRef.current
        ) {
          clearConversationUi();
          sessionIdRef.current = data.session_id;
          generationIdRef.current = null;
          setSessionId(data.session_id);
          setSessionError("The session changed. The new chat is ready; send your message again.");
          return;
        }

        const isCurrentGeneration =
          !resetInFlightRef.current &&
          typeof data.session_id === "string" &&
          typeof data.generation_id === "string" &&
          data.session_id === sessionIdRef.current &&
          data.generation_id === generationIdRef.current;

        if (!isCurrentGeneration) {
          return;
        }

        if (
          data.type === "approval_required" &&
          typeof data.approval_id === "string" &&
          typeof data.session_id === "string" &&
          typeof data.generation_id === "string"
        ) {
          setPendingApproval({
            approval_id: data.approval_id,
            session_id: data.session_id,
            generation_id: data.generation_id,
            title: data.title || "Confirm execution",
            toolName: data.tool_name || "tool",
            language: data.arguments?.language || "text",
            code: data.arguments?.code || (data.arguments ? JSON.stringify(data.arguments, null, 2) : ""),
          });
          setApprovalSubmitting(false);
        } else if (data.type === "started") {
          setStreaming(true);
          setMessages((prev) => prev.map((message) =>
            message.generationId === data.generation_id
              ? { ...message, status: "streaming" }
              : message
          ));
        } else if (data.type === "activity") {
          const line = formatActivity(data.activity);
          if (line) {
            setMessages((prev) => prev.map((message) =>
              message.role === "assistant" && message.generationId === data.generation_id
                ? { ...message, thoughtProcess: `${message.thoughtProcess || ""}${message.thoughtProcess ? "\n" : ""}${line}` }
                : message
            ));
          }
        } else if (data.type === "chunk") {
          const chunkText = typeof data.text === "string" ? data.text : "";
          const channel = data.channel === "thought" ? "thought" : "answer";
          setMessages((prev) => prev.map((message) => {
            if (
              message.role !== "assistant" ||
              message.generationId !== data.generation_id
            ) {
              return message;
            }
            return channel === "thought"
              ? {
                  ...message,
                  thoughtProcess: `${message.thoughtProcess || ""}${chunkText}`,
                }
              : {
                  ...message,
                  content: message.content + chunkText,
                };
          }));
        } else if (data.type === "done") {
          setPendingApproval(null);
          setApprovalSubmitting(false);
          setStreaming(false);
          setMessages((prev) => prev.map((message) =>
            message.generationId === data.generation_id
              ? { ...message, status: "done" }
              : message
          ));
        } else if (data.type === "error") {
          setPendingApproval(null);
          setApprovalSubmitting(false);
          setStreaming(false);
          setMessages((prev) => prev.map((message) =>
            message.generationId === data.generation_id
              ? {
                  ...message,
                  status: "error",
                  content: `${message.content}${message.content ? "\n\n" : ""}Error: ${data.text || "response unavailable"}`,
                }
              : message
          ));
        }
      };

      ws.onclose = () => {
        if (disposed || wsRef.current !== ws) return;

        wsRef.current = null;
        socketConnectedRef.current = false;
        acceptInitialSessionRef.current = true;
        connectionLostDuringGenerationRef.current ||= isStreamingRef.current;
        generationIdRef.current = null;
        setStreaming(false);
        console.log("Disconnected from Python daemon");
        if (!resetInFlightRef.current) {
          setSessionError("Connection to Jarvs Spotlight was interrupted. Reconnecting…");
        }

        const delay = Math.min(500 * (2 ** Math.min(retryAttempt, 4)), 5_000);
        retryAttempt += 1;
        reconnectTimer = window.setTimeout(() => void connect(), delay);
      };
      ws.onerror = () => {
        if (disposed || wsRef.current !== ws) return;

        socketConnectedRef.current = false;
        if (!resetInFlightRef.current) {
          setSessionError("Could not connect to the Jarvs Spotlight service. Retrying…");
        }
        ws.close();
      };
    };

    void connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) {
        window.clearTimeout(reconnectTimer);
      }
      const ws = wsRef.current;
      wsRef.current = null;
      socketConnectedRef.current = false;
      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onclose = null;
        ws.onerror = null;
        if (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN) {
          ws.close();
        }
      }
    };
  }, [clearConversationUi, setStreaming, startNewSession]);

  useEffect(() => {
    if (!isTauri()) return;

    let disposed = false;
    let unlisten: (() => void) | undefined;

    listen<NewSessionRequest>("jarvs://new-session", (event) => {
      void startNewSession({
        hideAfter: event.payload?.hideAfter === true,
        confirmIfStreaming: event.payload?.source !== "window-close",
      });
    }).then((cleanup) => {
      if (disposed) {
        cleanup();
      } else {
        unlisten = cleanup;
      }
    }).catch((error) => {
      console.error("Failed to listen for session reset requests", error);
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [startNewSession]);

  useEffect(() => {
    if (!isTauri()) return;

    let disposed = false;
    let unlisten: (() => void) | undefined;

    listen<VoiceRecordingRequest>("jarvs://voice-recording", (event) => {
      if (event.payload?.action === "start") {
        void startVoiceRecording();
      } else if (event.payload?.action === "stop") {
        stopVoiceRecording();
      }
    }).then((cleanup) => {
      if (disposed) {
        cleanup();
      } else {
        unlisten = cleanup;
      }
    }).catch((error) => {
      console.error("Failed to listen for voice shortcut", error);
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [startVoiceRecording, stopVoiceRecording]);

  useEffect(() => () => {
    const recorder = mediaRecorderRef.current;
    if (recorder) {
      recorder.onstop = null;
      recorder.ondataavailable = null;
      if (recorder.state === "recording") recorder.stop();
    }
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
  }, []);

  useEffect(() => {
    if (!isTauri()) return;

    let disposed = false;
    let unlisten: (() => void) | undefined;

    listen<NativeErrorEvent>("jarvs://native-error", (event) => {
      if (!disposed) {
        setSessionError(`${event.payload.component} error: ${event.payload.message}`);
      }
    }).then((cleanup) => {
      if (disposed) {
        cleanup();
      } else {
        unlisten = cleanup;
      }
    }).catch((error) => {
      console.error("Failed to listen for native errors", error);
    });

    invoke<DaemonStatus>("daemon_status").then((status) => {
      if (disposed) return;
      if (!status.running && !status.starting) {
        setSessionError("The Python service did not start. Check the Jarvs Spotlight logs.");
      } else if (!status.shortcutRegistered) {
        setSessionError("The Win+Alt+Space shortcut is unavailable; use the tray icon.");
      } else if (status.voiceShortcutRegistered === false) {
        setSessionError("The F4 shortcut is unavailable; use the microphone button.");
      }
    }).catch((error) => {
      console.error("Failed to read native status", error);
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!isTauri()) return;

    let disposed = false;
    let unlisten: (() => void) | undefined;

    getCurrentWindow().onFocusChanged(({ payload: focused }) => {
      if (focused && resetInFlightRef.current) {
        pendingHideAfterResetRef.current = false;
        resetRecoveryHideAfterRef.current = false;
      }
      if (
        focused &&
        !showSettingsRef.current &&
        !resetInFlightRef.current &&
        sessionIdRef.current
      ) {
        window.setTimeout(() => inputRef.current?.focus(), 0);
      }
    }).then((cleanup) => {
      if (disposed) {
        cleanup();
      } else {
        unlisten = cleanup;
      }
    }).catch((error) => {
      console.error("Failed to listen for window focus", error);
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  const handleModelChange = async (e: React.ChangeEvent<HTMLSelectElement>) => {
    const newModelId = e.target.value;
    setActiveModel(newModelId);
    // Find the selected model and populate the correct fields
    const selectedModel = models.find(m => m.id === newModelId);
    if (selectedModel) {
      const provider = selectedModel.provider || "cloud";
      setActiveProvider(provider);
      setModelString(selectedModel.model_string || "");
      if (provider === "local") {
        setServerUrl(selectedModel.api_base || "http://localhost:1234/v1");
        setApiKey("");
        setApiKeyConfigured(false);
      } else {
        setApiKey("");
        setApiKeyConfigured(selectedModel.api_key_configured === true);
        setServerUrl("");
      }
    }
    try {
      const response = await daemonFetch("/settings/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: newModelId }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch (error) {
      console.error("Failed to switch model", error);
      setSessionError("The model could not be changed.");
    }
  };

  const saveSettings = async () => {
    try {
      // Send the correct payload based on provider type
      const payload: Record<string, string> = {
        model_id: activeModel,
        theme,
        workspace_path: workspacePath,
        model_string: modelString,
        execution_policy: executionPolicy,
        cloud_workspace_egress: cloudWorkspaceEgress,
      };
      if (activeProvider === "local") {
        payload.api_base = serverUrl;
        // Never send api_key for local models
      } else if (apiKey) {
        payload.api_key = apiKey;
        // Never send api_base for cloud models
      }
      const response = await daemonFetch("/settings/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const saved = await response.json();
      setExecutionPolicy(saved.execution_policy || executionPolicy);
      setApiKey("");
      await loadSettings();
      setShowSettings(false);
    } catch (error) {
      console.error("Failed to save settings", error);
      setSessionError("Settings were not saved. Correct the data and try again.");
    }
  };

  const handleOpenSettings = () => {
    setShowSettings(true);
    void loadSettings();
    void loadPersonalData();
  };

  const loadPersonalData = async () => {
    try {
      const [memoryResponse, routineResponse] = await Promise.all([
        daemonFetch("/memory"),
        daemonFetch("/routines"),
      ]);
      if (!memoryResponse.ok || !routineResponse.ok) throw new Error("Personal data API unavailable");
      const memoryData = await memoryResponse.json();
      const routineData = await routineResponse.json();
      setMemoryItems(memoryData.memories || []);
      setRoutines(routineData.routines || []);
      setPersonalDataError(null);
    } catch (error) {
      console.error("Failed to load personal data", error);
      setPersonalDataError("Memory and routines are unavailable.");
    }
  };

  const addMemoryItem = async () => {
    if (!newMemory.content.trim()) return;
    try {
      const response = await daemonFetch("/memory", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          category: newMemory.category,
          content: newMemory.content.trim(),
          provenance: "user_ui_explicit",
          confidence: 1,
          consent: true,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      setNewMemory({ category: "preference", content: "" });
      await loadPersonalData();
    } catch (error) {
      console.error("Failed to create memory", error);
      setPersonalDataError("The memory was not saved; make sure it does not contain secrets.");
    }
  };

  const updateMemoryItem = async (item: MemoryItem) => {
    try {
      const response = await daemonFetch(`/memory/${encodeURIComponent(item.memory_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          category: item.category,
          content: item.content,
          confidence: item.confidence,
          provenance: "user_ui_edit",
          consent: true,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      await loadPersonalData();
    } catch (error) {
      console.error("Failed to update memory", error);
      setPersonalDataError("The memory update was not saved.");
    }
  };

  const deleteMemoryItem = async (memoryId: string) => {
    try {
      const response = await daemonFetch(`/memory/${encodeURIComponent(memoryId)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await response.text());
      await loadPersonalData();
    } catch (error) {
      console.error("Failed to delete memory", error);
      setPersonalDataError("The memory was not deleted.");
    }
  };

  const saveRoutine = async () => {
    try {
      const manifest = JSON.parse(routineEditor) as Record<string, unknown>;
      const path = editingRoutineId ? `/routines/${encodeURIComponent(editingRoutineId)}` : "/routines";
      const response = await daemonFetch(path, {
        method: editingRoutineId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ manifest }),
      });
      if (!response.ok) throw new Error(await response.text());
      setRoutineEditor("");
      setEditingRoutineId(null);
      await loadPersonalData();
    } catch (error) {
      console.error("Failed to save routine", error);
      setPersonalDataError("The routine manifest is invalid or failed tool validation.");
    }
  };

  const deleteRoutine = async (routineId: string) => {
    try {
      const response = await daemonFetch(`/routines/${encodeURIComponent(routineId)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await response.text());
      if (editingRoutineId === routineId) {
        setEditingRoutineId(null);
        setRoutineEditor("");
      }
      await loadPersonalData();
    } catch (error) {
      console.error("Failed to delete routine", error);
      setPersonalDataError("The routine was not deleted.");
    }
  };

  const copyPersonalExport = async () => {
    try {
      const [memoryResponse, routineResponse] = await Promise.all([
        daemonFetch("/memory/export"),
        daemonFetch("/routines/export"),
      ]);
      if (!memoryResponse.ok || !routineResponse.ok) throw new Error("Export failed");
      const value = {
        memory: await memoryResponse.json(),
        routines: await routineResponse.json(),
      };
      await navigator.clipboard.writeText(JSON.stringify(value, null, 2));
      setPersonalDataError("Export copied to the clipboard.");
    } catch (error) {
      console.error("Failed to export personal data", error);
      setPersonalDataError("The export could not be copied.");
    }
  };

  const handleDeleteModel = async () => {
    if (!activeModel) return;
    try {
      const response = await daemonFetch("/settings/models/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: activeModel }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await loadSettings();
    } catch (error) {
      console.error("Failed to delete model", error);
      setSessionError("The model could not be deleted.");
    }
  };

  const submitAddModel = async () => {
    try {
      const payload = {
        name: newModel.name,
        provider: newModel.provider,
        model_string: newModel.modelString,
        api_key: newModel.provider === "cloud" ? newModel.urlOrKey : null,
        api_base: newModel.provider === "local" ? newModel.urlOrKey : null,
      };
      const response = await daemonFetch("/settings/models/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setShowAddModel(false);
      setNewModel({ name: "", provider: "cloud", modelString: "", urlOrKey: "" });
      await loadSettings();
    } catch (error) {
      console.error("Failed to add model", error);
      setSessionError("The model could not be added.");
    }
  };

  const handleQuit = async () => {
    try {
      await invoke("quit_app");
    } catch (error) {
      console.error("Failed to quit Jarvs", error);
      setSessionError("Jarvs Spotlight could not be closed.");
      setShowSettings(false);
    }
  };

  const handleAttachFile = async () => {
    try {
      const selected = await invoke<Attachment | null>("pick_attachment");
      if (selected) {
        setAttachments(prev => [...prev, selected]);
      }
    } catch (e) {
      console.error("Failed to open dialog", e);
    }
  };

  const respondToApproval = async (decision: "approve" | "deny") => {
    if (!pendingApproval || approvalSubmitting) return;
    setApprovalSubmitting(true);
    try {
      const response = await daemonFetch(
        `/approvals/${encodeURIComponent(pendingApproval.approval_id)}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            decision,
            session_id: pendingApproval.session_id,
            generation_id: pendingApproval.generation_id,
          }),
        }
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setPendingApproval(null);
    } catch (error) {
      console.error("Failed to resolve approval", error);
      setApprovalSubmitting(false);
      setSessionError("The decision was not applied; execution remains paused.");
    }
  };

  const removeAttachment = (idx: number) => {
    setAttachments(prev => prev.filter((_, i) => i !== idx));
  };

  const toggleThought = (messageId: string) => {
    setExpandedThoughts((previous) => {
      const next = new Set(previous);
      if (next.has(messageId)) {
        next.delete(messageId);
      } else {
        next.add(messageId);
      }
      return next;
    });
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if ((!input.trim() && attachments.length === 0) || isStreamingRef.current || isResetting) return;

    const ws = wsRef.current;
    const currentSessionId = sessionIdRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN || !currentSessionId) {
      setSessionError("Jarvs Spotlight is not ready yet. Wait for the connection and try again.");
      return;
    }

    const userMsg = input.trim();
    const msgAttachments = attachments.length > 0 ? [...attachments] : undefined;
    const generationId = createGenerationId();
    generationIdRef.current = generationId;
    setSessionError(null);
    setMessages((prev) => [
      ...prev,
      {
        id: `${generationId}:user`,
        role: "user",
        content: userMsg,
        attachments: msgAttachments,
        generationId,
      },
      {
        id: `${generationId}:assistant`,
        role: "assistant",
        content: "",
        generationId,
        thoughtProcess: "",
        status: "streaming",
      },
    ]);
    setStreaming(true);
    try {
      ws.send(JSON.stringify({
        prompt: userMsg,
        attachments,
        session_id: currentSessionId,
        generation_id: generationId,
      }));
    } catch (error) {
      console.error("Failed to send prompt", error);
      generationIdRef.current = null;
      setStreaming(false);
      setMessages((prev) => prev.map((message) =>
        message.generationId === generationId && message.role === "assistant"
          ? {
              ...message,
              status: "error",
              content: "Error: message could not be sent.",
            }
          : message
      ));
      setSessionError("Message could not be sent. Check the connection to Jarvs Spotlight and try again.");
    }

    setInput("");
    setAttachments([]);
  };

  return (
    <div
      className="app-shell absolute inset-0 flex flex-col bg-jarvs-bg text-jarvs-text font-sans select-none overflow-hidden"
      data-theme={getThemeAttribute(theme)}
      data-window-mode={windowMode}
    >

      {pendingApproval && (
        <div className="settings-overlay absolute inset-0 z-[60] flex items-center justify-center p-3 overflow-hidden animate-fade-in">
          <div className="settings-panel rounded-xl animate-slide-up flex max-h-[calc(100dvh-24px)] w-[min(520px,calc(100vw-24px))] flex-col overflow-hidden" role="dialog" aria-modal="true" aria-label="Confirm execution">
            <div className="shrink-0 border-b border-jarvs-border px-4 pb-3 pt-4 sm:px-5">
              <div className="flex items-center gap-2">
                <AlertTriangle size={15} className="text-amber-400" />
                <h2 className="text-sm font-semibold">{pendingApproval.title}</h2>
              </div>
              <p className="mt-1 text-[11px] text-jarvs-text-muted">{pendingApproval.toolName}</p>
            </div>
            <div className="min-h-0 flex-1 overflow-auto px-4 py-3 sm:px-5">
              <pre className="whitespace-pre-wrap break-words rounded-lg border border-jarvs-border bg-black/20 p-3 text-[11px] leading-relaxed select-text"><code>{pendingApproval.code || "(no details available)"}</code></pre>
            </div>
            <div className="flex shrink-0 justify-end gap-2 border-t border-jarvs-border-strong px-4 py-3 sm:px-5">
              <button disabled={approvalSubmitting} onClick={() => void respondToApproval("deny")} className="danger-button px-3.5 py-1.5 rounded-lg text-sm disabled:opacity-50">Deny</button>
              <button disabled={approvalSubmitting} onClick={() => void respondToApproval("approve")} className="accent-button px-3.5 py-1.5 rounded-lg bg-jarvs-accent text-sm font-medium disabled:opacity-50">Approve once</button>
            </div>
          </div>
        </div>
      )}

      {/* Settings Modal */}
      {showSettings && (
        <div className="settings-overlay absolute inset-0 z-50 flex items-center justify-center p-3 overflow-hidden animate-fade-in">
          <div
            ref={settingsPanelRef}
            className="settings-panel rounded-xl animate-slide-up flex flex-col min-h-0 overflow-hidden"
            style={{
              zoom: zoomLevel,
              width: `min(400px, calc(${100 / zoomLevel}vw - ${24 / zoomLevel}px))`,
              maxHeight: `calc(${100 / zoomLevel}dvh - ${24 / zoomLevel}px)`,
            }}
            role="dialog"
            aria-modal="true"
            aria-label={showAddModel ? "Add model" : "Jarvs Spotlight settings"}
          >
            <div className="flex justify-between items-center shrink-0 px-4 pt-4 pb-3 sm:px-5 border-b border-jarvs-border">
              <h2 className="text-base font-semibold tracking-wide">
                {showAddModel ? "Add New Model" : "Jarvs Spotlight Settings"}
              </h2>
              <button
                type="button"
                onClick={() => setShowSettings(false)}
                className="settings-icon-button p-1 rounded-md transition text-jarvs-text-muted hover:text-jarvs-text"
                title="Close"
                aria-label="Close settings"
              >
                <X size={14} />
              </button>
            </div>

            <div className="settings-panel__body min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 sm:px-5">
              {!showAddModel ? (
                <>
                {/* General Settings */}
                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Theme:</label>
                <select
                  value={theme}
                  onChange={(e) => setTheme(normalizeThemeName(e.target.value))}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm transition focus:border-jarvs-accent"
                  aria-label="Theme"
                  aria-describedby="theme-description"
                >
                  {THEME_OPTIONS.map((option) => (
                    <option key={option.name} value={option.name}>{option.label}</option>
                  ))}
                </select>
                <p id="theme-description" className="theme-description mb-3 mt-1.5 text-[11px] leading-relaxed text-jarvs-text-muted">
                  {THEME_OPTIONS.find((option) => option.name === theme)?.description}
                </p>

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Default Workspace Path:</label>
                <input
                  type="text"
                  value={workspacePath}
                  onChange={(e) => setWorkspacePath(e.target.value)}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-3 transition focus:border-jarvs-accent"
                />

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Execution Policy:</label>
                <select
                  value={executionPolicy}
                  onChange={(event) => setExecutionPolicy(event.target.value as ExecutionPolicy)}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm transition focus:border-jarvs-accent"
                  aria-describedby="execution-policy-description"
                >
                  <option value="always_ask">Ask before every execution (recommended)</option>
                  <option value="auto_run">Run automatically (advanced mode)</option>
                  <option value="deny_all">Block all execution</option>
                </select>
                <p id="execution-policy-description" className="theme-description mb-5 mt-1.5 text-[11px] leading-relaxed text-jarvs-text-muted">
                  {executionPolicy === "always_ask" && "Every tool or code block remains paused until you approve it."}
                  {executionPolicy === "auto_run" && "Allowed deterministic actions run without confirmation. Python and PowerShell still require one-time approval until a strong system sandbox is available."}
                  {executionPolicy === "deny_all" && "Jarvs Spotlight can answer, but it cannot execute tools or code."}
                </p>

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Workspace Data Sent to Cloud Models:</label>
                <select
                  value={cloudWorkspaceEgress}
                  onChange={(event) => setCloudWorkspaceEgress(event.target.value as CloudWorkspaceEgress)}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm transition focus:border-jarvs-accent"
                  aria-describedby="workspace-egress-description"
                >
                  <option value="deny">Do not send (recommended)</option>
                  <option value="allow">Allow sending tool results</option>
                </select>
                <p id="workspace-egress-description" className="theme-description mb-5 mt-1.5 text-[11px] leading-relaxed text-jarvs-text-muted">
                  This setting is separate from execution: a file can be read locally without sending its contents to the cloud provider.
                </p>

                <details className="mb-4 rounded-lg border border-jarvs-border-strong p-3">
                  <summary className="cursor-pointer text-sm font-semibold text-jarvs-text">Personal Memory</summary>
                  <p className="mb-3 mt-2 text-[11px] leading-relaxed text-jarvs-text-muted">
                    Only items entered or confirmed here are saved. Conversations, secrets, and reasoning are not stored automatically.
                  </p>
                  <div className="flex gap-2">
                    <select
                      value={newMemory.category}
                      onChange={(event) => setNewMemory((current) => ({ ...current, category: event.target.value }))}
                      className="settings-control w-32 border border-jarvs-border-strong rounded-lg p-2 text-xs"
                    >
                      <option value="preference">Preference</option>
                      <option value="profile">Profile</option>
                      <option value="episode">Episode</option>
                    </select>
                    <input
                      value={newMemory.content}
                      onChange={(event) => setNewMemory((current) => ({ ...current, content: event.target.value }))}
                      className="settings-control min-w-0 flex-1 border border-jarvs-border-strong rounded-lg p-2 text-xs"
                      placeholder="Information to remember…"
                    />
                    <button type="button" onClick={() => void addMemoryItem()} className="accent-button rounded-lg px-3 text-xs">Save</button>
                  </div>
                  <div className="mt-3 space-y-2">
                    {memoryItems.map((item, index) => (
                      <div key={item.memory_id} className="rounded-lg border border-jarvs-border p-2">
                        <div className="mb-1 flex items-center justify-between gap-2 text-[10px] text-jarvs-text-muted">
                          <span>{item.category} · v{item.version} · {item.provenance}</span>
                          <span>{Math.round(item.confidence * 100)}%</span>
                        </div>
                        <textarea
                          value={item.content}
                          onChange={(event) => setMemoryItems((current) => current.map((value, position) => position === index ? { ...value, content: event.target.value } : value))}
                          className="settings-control min-h-14 w-full resize-y rounded border border-jarvs-border-strong p-2 text-xs"
                        />
                        <div className="mt-1 flex justify-end gap-2">
                          <button type="button" onClick={() => void deleteMemoryItem(item.memory_id)} className="danger-button rounded px-2 py-1 text-[10px]">Delete</button>
                          <button type="button" onClick={() => void updateMemoryItem(item)} className="settings-secondary rounded px-2 py-1 text-[10px]">Update</button>
                        </div>
                      </div>
                    ))}
                    {memoryItems.length === 0 && <p className="text-[11px] text-jarvs-text-muted">No saved memories.</p>}
                  </div>
                </details>

                <details className="mb-5 rounded-lg border border-jarvs-border-strong p-3">
                  <summary className="cursor-pointer text-sm font-semibold text-jarvs-text">Versioned Routines</summary>
                  <p className="mb-2 mt-2 text-[11px] leading-relaxed text-jarvs-text-muted">
                    Manifests can only combine registered tools. Every save is validated and creates a new version; executable code is not accepted.
                  </p>
                  <div className="space-y-1.5">
                    {routines.map((routine) => (
                      <div key={routine.routine_id} className="flex items-center justify-between gap-2 rounded border border-jarvs-border p-2 text-xs">
                        <button
                          type="button"
                          className="min-w-0 flex-1 truncate text-left text-jarvs-text"
                          onClick={() => {
                            setEditingRoutineId(routine.routine_id);
                            setRoutineEditor(JSON.stringify(routine.manifest, null, 2));
                          }}
                        >
                          {routine.name} · v{routine.version}
                        </button>
                        <button type="button" onClick={() => void deleteRoutine(routine.routine_id)} className="danger-button rounded px-2 py-1 text-[10px]">Delete</button>
                      </div>
                    ))}
                  </div>
                  <textarea
                    value={routineEditor}
                    onChange={(event) => setRoutineEditor(event.target.value)}
                    className="settings-control mt-3 min-h-32 w-full resize-y rounded border border-jarvs-border-strong p-2 font-mono text-[10px]"
                    placeholder={'{"schema_version":1,"name":"…","description":"…","input_schema":{"type":"object","properties":{}},"steps":[]}' }
                  />
                  <div className="mt-2 flex flex-wrap justify-between gap-2">
                    <button type="button" onClick={() => void copyPersonalExport()} className="settings-secondary rounded px-2 py-1 text-[10px]">Copy export</button>
                    <div className="flex gap-2">
                      {editingRoutineId && (
                        <button type="button" onClick={() => { setEditingRoutineId(null); setRoutineEditor(""); }} className="settings-secondary rounded px-2 py-1 text-[10px]">New</button>
                      )}
                      <button type="button" disabled={!routineEditor.trim()} onClick={() => void saveRoutine()} className="accent-button rounded px-2 py-1 text-[10px] disabled:opacity-50">
                        {editingRoutineId ? "Save new version" : "Validate and save"}
                      </button>
                    </div>
                  </div>
                  {personalDataError && <p className="mt-2 text-[11px] text-jarvs-text-muted">{personalDataError}</p>}
                </details>

                <div className="flex items-center gap-2 mb-3">
                  <Zap size={14} className="text-jarvs-accent" />
                  <h3 className="font-semibold text-sm text-jarvs-text">Model Configuration</h3>
                </div>

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Active Model:</label>
                <select
                  value={activeModel}
                  onChange={handleModelChange}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-1 transition focus:border-jarvs-accent"
                >
                  {models.length === 0 && <option value="" disabled>Loading models...</option>}
                  {models.map(m => (
                    <option key={m.id} value={m.id}>
                      {m.name} ({m.provider === 'cloud' ? 'Cloud' : 'Local'})
                    </option>
                  ))}
                </select>

                <p className="text-[11px] text-jarvs-text-muted/80 mb-3 flex items-center gap-1.5">
                  <span className="flex items-center gap-1">
                    {activeProvider === "cloud" ? <><Cloud size={11} /> Cloud Model — requires an API key</> : <><Monitor size={11} /> Local Model — connects to a local inference server</>}
                  </span>
                </p>

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Model String:</label>
                <input
                  type="text"
                  value={modelString}
                  onChange={(e) => setModelString(e.target.value)}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-3 transition focus:border-jarvs-accent"
                />

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">
                  {activeProvider === "local" ? "Server URL:" : "API Key:"}
                </label>
                {activeProvider === "local" ? (
                  <input
                    type="text"
                    value={serverUrl}
                    onChange={(e) => setServerUrl(e.target.value)}
                    className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-3 transition focus:border-jarvs-accent"
                    placeholder="http://localhost:1234/v1"
                  />
                ) : (
                  <input
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-3 transition focus:border-jarvs-accent"
                    placeholder={apiKeyConfigured ? "Configured — leave blank to keep it" : "Not configured"}
                  />
                )}

                <div className="flex justify-between items-center mb-6">
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => setShowAddModel(true)} className="px-2 py-1 border border-jarvs-border-strong border-dashed rounded text-xs text-jarvs-accent hover:bg-jarvs-accent/10 transition">+ Add Model</button>
                    <button onClick={handleDeleteModel} className="danger-button px-2 py-1 border rounded text-xs transition flex items-center gap-1"><Trash2 size={12} /> Delete</button>
                  </div>
                </div>
                </>
              ) : (
                <>
                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Display Name:</label>
                <input
                  type="text"
                  value={newModel.name}
                  onChange={(e) => setNewModel(prev => ({...prev, name: e.target.value}))}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-3 transition focus:border-jarvs-accent"
                  placeholder="e.g. My Local Qwen"
                />

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Provider:</label>
                <select
                  value={newModel.provider}
                  onChange={(e) => setNewModel(prev => ({...prev, provider: e.target.value}))}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-3 transition focus:border-jarvs-accent"
                >
                  <option value="cloud">Cloud (OpenAI or Gemini)</option>
                  <option value="local">Local (LM Studio, Ollama, or compatible)</option>
                </select>

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">Model String:</label>
                <input
                  type="text"
                  value={newModel.modelString}
                  onChange={(e) => setNewModel(prev => ({...prev, modelString: e.target.value}))}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-3 transition focus:border-jarvs-accent"
                  placeholder="e.g. openai/qwen2.5-coder-7b"
                />

                <label className="block text-xs font-semibold text-jarvs-text mb-1.5 tracking-wider">
                  {newModel.provider === "local" ? "Server URL:" : "API Key:"}
                </label>
                <input
                  type="text"
                  value={newModel.urlOrKey}
                  onChange={(e) => setNewModel(prev => ({...prev, urlOrKey: e.target.value}))}
                  className="settings-control w-full border border-jarvs-border-strong rounded-lg p-2 outline-none text-sm mb-6 transition focus:border-jarvs-accent"
                  placeholder={newModel.provider === "local" ? "http://localhost:1234/v1" : "sk-..."}
                />
                </>
              )}
            </div>

            <div className={`settings-panel__footer shrink-0 flex flex-wrap gap-2 border-t border-jarvs-border-strong px-4 py-3 sm:px-5 ${showAddModel ? "justify-end" : "justify-between"}`}>
              {!showAddModel ? (
                <>
                  <button onClick={() => void handleQuit()} className="danger-button danger-button--filled px-3.5 py-1.5 rounded-lg transition text-sm font-medium">Quit Jarvs Spotlight</button>
                  <button onClick={saveSettings} className="px-4 py-1.5 rounded-lg bg-green-600/80 hover:bg-green-600 transition text-sm font-medium text-white">Save & Apply</button>
                </>
              ) : (
                <>
                  <button onClick={() => setShowAddModel(false)} className="settings-secondary px-3.5 py-1.5 rounded-lg transition text-sm text-jarvs-text-muted hover:text-jarvs-text">Cancel</button>
                  <button onClick={submitAddModel} className="accent-button px-3.5 py-1.5 rounded-lg bg-jarvs-accent hover:bg-jarvs-accent-hover transition text-sm font-medium">Add</button>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Titlebar */}
      <div data-tauri-drag-region className="titlebar flex justify-between items-center gap-2 px-3 sm:px-4 py-2 shrink-0 cursor-default">
        <div className="min-w-0 flex-1 text-sm font-medium flex items-center gap-2 pointer-events-none">
          <img className="brand-logo shrink-0" src="/jarvs-logo.png" alt="" aria-hidden="true" draggable={false} />
          <span className="brand-mark shrink-0">Jarvs Spotlight</span>
          {activeModel && (
            <span className="min-w-0 truncate text-[11px] text-jarvs-text-muted ml-1 font-normal">· {activeModel}</span>
          )}
        </div>
        <div className="flex items-center gap-0.5 shrink-0">
          <button
            onClick={() => void startNewSession()}
            disabled={isResetting}
            className="px-2 py-1 mr-1 flex items-center gap-1 hover:bg-jarvs-surface-hover rounded-md transition text-jarvs-text-muted hover:text-jarvs-text disabled:opacity-40 disabled:cursor-wait"
            title="New chat"
            aria-label="Start a new chat"
          >
            <Plus size={13} />
            <span className="hidden min-[480px]:inline text-[11px]">New chat</span>
          </button>
          <button ref={settingsButtonRef} onClick={handleOpenSettings} className="p-1.5 hover:bg-jarvs-surface-hover rounded-md transition text-jarvs-text-muted hover:text-jarvs-text" title="Settings" aria-label="Open settings">
            <Settings size={13} />
          </button>
          <button onClick={() => void getCurrentWindow().hide()} className="p-1.5 hover:bg-jarvs-surface-hover rounded-md transition text-jarvs-text-muted hover:text-jarvs-text" title="Hide Jarvs Spotlight" aria-label="Hide Jarvs Spotlight">
            <Minus size={13} />
          </button>
          <button onClick={() => void getCurrentWindow().close()} className="p-1.5 hover:bg-red-500/60 rounded-md transition text-jarvs-text-muted hover:text-white" title="Close chat" aria-label="Close the chat and hide Jarvs Spotlight">
            <X size={13} />
          </button>
        </div>
      </div>

      {/* Zoomable content area */}
      <div className="flex-1 flex flex-col min-h-0" style={{ zoom: zoomLevel }}>

      {sessionError && (
        <div role="alert" className="session-alert mx-4 mt-3 px-3 py-2 flex items-start gap-2 rounded-lg border text-xs shrink-0">
          <AlertTriangle size={14} className="session-alert__icon mt-0.5 shrink-0" />
          <span className="flex-1">{sessionError}</span>
          <button
            type="button"
            onClick={() => setSessionError(null)}
            className="session-alert__dismiss p-0.5 rounded"
            title="Dismiss alert"
            aria-label="Dismiss alert"
          >
            <X size={12} />
          </button>
        </div>
      )}

      {/* Chat History */}
      <div className="chat-history flex-1 overflow-y-auto px-4 py-3 space-y-3 flex flex-col min-h-0">
        {messages.length === 0 && (
          <div className="jarvs-empty m-auto text-jarvs-text-muted text-sm flex flex-col items-center gap-2.5">
            <div className="jarvs-core" aria-hidden="true">
              <span className="jarvs-core__ring jarvs-core__ring--outer" />
              <span className="jarvs-core__ring jarvs-core__ring--middle" />
              <span className="jarvs-core__center">
                <span className="jarvs-core__signal">
                  <i /><i /><i /><i /><i />
                </span>
              </span>
            </div>
            <p className="jarvs-empty__eyebrow">JARVS SPOTLIGHT</p>
            <p className="text-xs m-0">How can I help?</p>
          </div>
        )}
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`p-3 rounded-xl max-w-[85%] animate-slide-up ${
              msg.role === "user"
                ? "msg-user text-jarvs-text self-end rounded-br-sm"
                : "message-markdown msg-assistant text-jarvs-text self-start rounded-bl-sm prose prose-sm max-w-none break-words prose-p:my-1 prose-headings:my-2 prose-pre:overflow-x-auto prose-code:break-all prose-table:text-xs prose-th:px-2 prose-th:py-1 prose-td:px-2 prose-td:py-1"
            }`}
          >
            {msg.role === "user" ? (
              <div className="flex flex-col gap-1.5">
                {msg.content && (
                  <p className="whitespace-pre-wrap text-sm m-0 leading-relaxed">{msg.content}</p>
                )}
                {msg.attachments && msg.attachments.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-0.5">
                    {msg.attachments.map((att, i) => (
                      <span key={i} className="attachment-pill">
                        <Paperclip size={10} />
                        <span className="truncate max-w-[150px]">{att.displayName}</span>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                <div className="thought-process not-prose">
                  <button
                    type="button"
                    onClick={() => toggleThought(msg.id)}
                    className="flex items-center gap-1.5 text-jarvs-text-muted text-xs px-1 py-0.5 hover:text-jarvs-text transition cursor-pointer bg-transparent border-none outline-none"
                    aria-expanded={expandedThoughts.has(msg.id)}
                  >
                    {expandedThoughts.has(msg.id) ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                    {msg.status === "streaming" && (
                      <span className="w-1.5 h-1.5 rounded-full bg-jarvs-accent ai-pulse shrink-0" />
                    )}
                    <span>{msg.status === "streaming" ? "Activity…" : "Activity trace"}</span>
                  </button>
                  {expandedThoughts.has(msg.id) && (
                    <div className="thought-process__content ml-5 mt-1 pl-2 border-l border-jarvs-border text-[11px] text-jarvs-text-muted">
                      {msg.thoughtProcess ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {msg.thoughtProcess}
                        </ReactMarkdown>
                      ) : (
                        <p className="m-0 py-1">
                          {msg.status === "streaming"
                            ? "Processing response…"
                            : "No execution trace was emitted for this response."}
                        </p>
                      )}
                    </div>
                  )}
                </div>
                {msg.content && (
                  <div className="msg-content-scroll">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {msg.content}
                    </ReactMarkdown>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
        <div ref={endOfMessagesRef} />
      </div>

      {/* Attachments */}
      {attachments.length > 0 && (
        <div className="px-4 py-2 border-t border-jarvs-border flex gap-2 overflow-x-auto shrink-0">
          {attachments.map((att, idx) => (
            <div key={idx} className="flex items-center gap-1.5 bg-jarvs-surface border border-jarvs-border rounded-md px-2 py-1 text-xs">
              <span className="truncate max-w-[140px] text-jarvs-text-muted">{att.displayName}</span>
              <button
                type="button"
                onClick={() => removeAttachment(idx)}
                className="hover:text-red-400 transition"
                aria-label={`Remove attachment ${att.displayName}`}
              >
                <X size={11}/>
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Input */}
      <div className="p-3 shrink-0">
        <form onSubmit={handleSubmit} className="input-glow flex gap-2 items-center bg-jarvs-surface border border-jarvs-border rounded-xl px-3 py-2 transition-all">
          <button
            type="button"
            onClick={handleAttachFile}
            className="p-1 hover:bg-jarvs-surface-hover rounded-md transition text-jarvs-text-muted hover:text-jarvs-text shrink-0"
            title="Attach file"
            aria-label="Attach file"
          >
            <Paperclip size={16} />
          </button>
          <button
            type="button"
            onClick={() => recordingRef.current ? stopVoiceRecording() : void startVoiceRecording()}
            disabled={isTranscribing || isResetting}
            className={`p-1 rounded-md transition shrink-0 disabled:opacity-50 ${
              isRecording
                ? "bg-red-500/20 text-red-400 animate-pulse"
                : "hover:bg-jarvs-surface-hover text-jarvs-text-muted hover:text-jarvs-text"
            }`}
            title={isRecording ? "Stop recording" : isTranscribing ? "Transcribing" : "Hold F4 or use the microphone"}
            aria-label={isRecording ? "Stop voice recording" : "Start voice recording"}
          >
            <Mic size={16} />
          </button>
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={isRecording ? "Recording…" : isTranscribing ? "Transcribing…" : isResetting ? "Starting a new chat..." : sessionId ? "Ask Jarvs Spotlight..." : "Connecting to Jarvs Spotlight..."}
            className="flex-1 bg-transparent text-jarvs-text text-sm border-none outline-none placeholder-jarvs-text-muted/50"
            disabled={isResetting || !sessionId}
            autoFocus
          />
        </form>
      </div>

      </div>{/* End zoomable content */}
    </div>
  );
}

export default App;
