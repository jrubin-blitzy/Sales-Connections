import { useCallback, useEffect, useState, type FormEvent, type JSX } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { LogIn, Mail, UserPlus } from "lucide-react";

import { useRegisterMutation, useLoginMutation } from "@/api/auth";
import { useSession, useSessionLoading } from "@/auth/AuthProvider";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

const DEFAULT_LOGIN_DESTINATION = "/feed";
const ALLOWED_DOMAIN = "@blitzy.com";

type Mode = "signin" | "register";

function parseNextDestination(rawNext: string | null): string {
  if (!rawNext) return DEFAULT_LOGIN_DESTINATION;
  let decoded: string;
  try {
    decoded = decodeURIComponent(rawNext);
  } catch {
    return DEFAULT_LOGIN_DESTINATION;
  }
  if (!decoded.startsWith("/") || decoded.startsWith("//")) return DEFAULT_LOGIN_DESTINATION;
  if (decoded.startsWith("/auth/") || decoded === "/login" || decoded.startsWith("/login?")) {
    return DEFAULT_LOGIN_DESTINATION;
  }
  return decoded;
}

export function LoginScreen(): JSX.Element {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const session = useSession();
  const sessionLoading = useSessionLoading();
  const loginMutation = useLoginMutation();
  const registerMutation = useRegisterMutation();

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState<string>("");
  const [password, setPassword] = useState<string>("");
  const [emailError, setEmailError] = useState<string | undefined>(undefined);
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);

  const nextDestination = parseNextDestination(searchParams.get("next"));

  const emailInputRef = useCallback((node: HTMLInputElement | null): void => {
    if (node !== null) node.focus();
  }, []);

  useEffect(() => {
    if (sessionLoading) return;
    if (session?.authenticated === true) {
      navigate(nextDestination, { replace: true });
    }
  }, [session, sessionLoading, nextDestination, navigate]);

  const validate = useCallback((): boolean => {
    setEmailError(undefined);
    setPasswordError(undefined);
    let valid = true;
    const trimmedEmail = email.trim().toLowerCase();
    if (!trimmedEmail) {
      setEmailError("Email is required.");
      valid = false;
    } else if (!trimmedEmail.endsWith(ALLOWED_DOMAIN)) {
      setEmailError("Only @blitzy.com email addresses are allowed.");
      valid = false;
    }
    if (!password) {
      setPasswordError("Password is required.");
      valid = false;
    } else if (password.length < 8) {
      setPasswordError("Password must be at least 8 characters.");
      valid = false;
    }
    return valid;
  }, [email, password]);

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>): Promise<void> => {
      event.preventDefault();
      if (!validate()) return;
      const trimmedEmail = email.trim().toLowerCase();
      try {
        if (mode === "register") {
          const namePart = trimmedEmail.split("@")[0] ?? "";
          const displayName = namePart
            .replace(/[._-]+/g, " ")
            .replace(/\b\w/g, (c) => c.toUpperCase());
          await registerMutation.mutateAsync({
            email: trimmedEmail,
            password,
            confirm_password: password,
            display_name: displayName,
          });
        } else {
          await loginMutation.mutateAsync({ email: trimmedEmail, password });
        }
        // Navigation is handled by the useEffect above once session updates
      } catch {
        // Errors are handled by mutation onError callbacks (toast)
      }
    },
    [email, password, mode, validate, loginMutation, registerMutation],
  );

  if (sessionLoading) {
    return (
      <section
        className="min-h-screen flex items-center justify-center bg-slate-50 px-4 py-8"
        aria-busy="true"
        data-testid="login-screen-loading"
      >
        <div className="text-sm text-slate-500">Loading...</div>
      </section>
    );
  }

  if (session?.authenticated === true) {
    return <section className="min-h-screen" aria-hidden="true" />;
  }

  const isPending = loginMutation.isPending || registerMutation.isPending;

  return (
    <section
      className="min-h-screen flex items-center justify-center bg-slate-50 px-4 py-8"
      aria-labelledby="login-heading"
      data-testid="login-screen"
    >
      <div className="w-full max-w-md bg-white border border-slate-200 rounded-lg shadow-card p-6 sm:p-8">
        <div className="text-center mb-6">
          <h1 id="login-heading" className="text-2xl font-semibold text-slate-900">
            {mode === "signin" ? "Sign in to Sales-Connections" : "Create your account"}
          </h1>
          <p className="mt-2 text-sm text-slate-600">
            {mode === "signin"
              ? "Enter your @blitzy.com credentials to continue."
              : "Register with your @blitzy.com email."}
          </p>
        </div>

        <form onSubmit={handleSubmit} noValidate data-testid="login-form">
          <div className="flex flex-col gap-4">
            <Input
              ref={emailInputRef}
              label="Email address"
              type="email"
              name="email"
              autoComplete="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              errorMessage={emailError}
              leftIcon={<Mail aria-hidden="true" />}
              placeholder="you@blitzy.com"
              data-testid="login-email"
            />

            <Input
              label="Password"
              type="password"
              name="password"
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              errorMessage={passwordError}
              placeholder="••••••••"
              data-testid="login-password"
            />

            <Button
              type="submit"
              variant="primary"
              size="md"
              fullWidth
              disabled={isPending}
              leftIcon={mode === "signin" ? <LogIn aria-hidden="true" /> : <UserPlus aria-hidden="true" />}
              data-testid="login-submit"
            >
              {isPending
                ? mode === "signin" ? "Signing in…" : "Creating account…"
                : mode === "signin" ? "Sign in" : "Create account"}
            </Button>
          </div>
        </form>

        <div className="mt-4 text-center">
          {mode === "signin" ? (
            <p className="text-sm text-slate-600">
              No account yet?{" "}
              <button
                type="button"
                className="text-brand-600 hover:underline font-medium"
                onClick={() => { setMode("register"); setEmailError(undefined); setPasswordError(undefined); }}
              >
                Create one
              </button>
            </p>
          ) : (
            <p className="text-sm text-slate-600">
              Already have an account?{" "}
              <button
                type="button"
                className="text-brand-600 hover:underline font-medium"
                onClick={() => { setMode("signin"); setEmailError(undefined); setPasswordError(undefined); }}
              >
                Sign in
              </button>
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
