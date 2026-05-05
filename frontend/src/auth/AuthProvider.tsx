/* eslint-disable react-refresh/only-export-components */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type JSX,
  type ReactNode,
} from "react";

import type { Session, SessionRead } from "@/schemas/auth";
import { USER_ROLE_VALUES } from "@/schemas/admin";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type UserRole = (typeof USER_ROLE_VALUES)[number];

export interface UseRoleReturn {
  role: UserRole | null;
  has: (target: UserRole) => boolean;
}

export interface AuthContextValue {
  session: SessionRead | null;
  isLoading: boolean;
  isError: boolean;
  login: (email: string) => void;
  logout: () => void;
}

export const AuthContext = createContext<AuthContextValue | undefined>(undefined);

// ---------------------------------------------------------------------------
// localStorage helpers
// ---------------------------------------------------------------------------

const STORAGE_EMAIL_KEY = "sc_user_email";
const STORAGE_ID_KEY = "sc_user_id";

function buildSession(email: string, userId: string): SessionRead {
  const namePart = email.split("@")[0] ?? "";
  const displayName = namePart
    .replace(/[._-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return {
    authenticated: true,
    user: {
      id: userId,
      email,
      display_name: displayName,
      role: "Contributor",
      created_at: new Date().toISOString(),
    },
  };
}

function readStoredSession(): SessionRead | null {
  try {
    const email = localStorage.getItem(STORAGE_EMAIL_KEY);
    const userId = localStorage.getItem(STORAGE_ID_KEY);
    if (!email || !userId || !email.endsWith("@blitzy.com")) return null;
    return buildSession(email, userId);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// AuthProvider component
// ---------------------------------------------------------------------------

export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  const [session, setSession] = useState<SessionRead | null>(() => readStoredSession());

  const login = useCallback((email: string): void => {
    const userId = crypto.randomUUID();
    localStorage.setItem(STORAGE_EMAIL_KEY, email);
    localStorage.setItem(STORAGE_ID_KEY, userId);
    setSession(buildSession(email, userId));
  }, []);

  const logout = useCallback((): void => {
    localStorage.removeItem(STORAGE_EMAIL_KEY);
    localStorage.removeItem(STORAGE_ID_KEY);
    setSession(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ session, isLoading: false, isError: false, login, logout }),
    [session, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// ---------------------------------------------------------------------------
// Internal helper
// ---------------------------------------------------------------------------

function useAuthContext(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === undefined) {
    throw new Error(
      "useSession/useSessionLoading/useRole must be used inside <AuthProvider>.",
    );
  }
  return ctx;
}

// ---------------------------------------------------------------------------
// Public hooks
// ---------------------------------------------------------------------------

export function useSession(): Session | null {
  return useAuthContext().session;
}

export function useSessionLoading(): boolean {
  return useAuthContext().isLoading;
}

export function useRole(): UseRoleReturn {
  const ctx = useAuthContext();
  const role: UserRole | null = ctx.session?.user.role ?? null;
  return useMemo<UseRoleReturn>(
    () => ({ role, has: (target: UserRole) => role === target }),
    [role],
  );
}

export function useLogin(): (email: string) => void {
  return useAuthContext().login;
}

export function useLogout(): { mutateAsync: () => Promise<void>; isPending: boolean } {
  const logout = useAuthContext().logout;
  return useMemo(
    () => ({ mutateAsync: async () => { logout(); }, isPending: false }),
    [logout],
  );
}
