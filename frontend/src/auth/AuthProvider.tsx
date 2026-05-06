/* eslint-disable react-refresh/only-export-components */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  type JSX,
  type ReactNode,
} from "react";

import { useLoginMutation, useLogoutMutation, useSessionQuery } from "@/api/auth";
import { USER_ROLE_VALUES } from "@/schemas/admin";
import type { Session, SessionRead } from "@/schemas/auth";

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
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

export const AuthContext = createContext<AuthContextValue | undefined>(undefined);

// ---------------------------------------------------------------------------
// AuthProvider component
// ---------------------------------------------------------------------------

export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  const sessionQuery = useSessionQuery();
  const loginMutation = useLoginMutation();
  const logoutMutation = useLogoutMutation();

  const session: SessionRead | null = sessionQuery.data?.authenticated
    ? sessionQuery.data
    : null;
  const isLoading = sessionQuery.isLoading;
  // 401 = unauthenticated (expected); anything else is a real error
  const isError = sessionQuery.isError && (sessionQuery.error?.status ?? 0) !== 401;

  const login = useCallback(
    async (email: string, password: string): Promise<void> => {
      await loginMutation.mutateAsync({ email, password });
    },
    [loginMutation],
  );

  const logout = useCallback((): void => {
    logoutMutation.mutate();
  }, [logoutMutation]);

  const value = useMemo<AuthContextValue>(
    () => ({ session, isLoading, isError, login, logout }),
    [session, isLoading, isError, login, logout],
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

export function useLogin(): (email: string, password: string) => Promise<void> {
  return useAuthContext().login;
}

export function useLogout(): { mutateAsync: () => Promise<void>; isPending: boolean } {
  const logout = useAuthContext().logout;
  return useMemo(
    () => ({ mutateAsync: async () => { logout(); }, isPending: false }),
    [logout],
  );
}
