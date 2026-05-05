import { useCallback, useEffect, useState, type FormEvent, type JSX } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { LogIn, Mail } from "lucide-react";

import { useLogin, useSession, useSessionLoading } from "@/auth/AuthProvider";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

const DEFAULT_LOGIN_DESTINATION = "/feed";
const ALLOWED_DOMAIN = "@blitzy.com";

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
  const login = useLogin();

  const [email, setEmail] = useState<string>("");
  const [emailError, setEmailError] = useState<string | undefined>(undefined);

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

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>): void => {
      event.preventDefault();
      setEmailError(undefined);

      const trimmed = email.trim().toLowerCase();

      if (!trimmed) {
        setEmailError("Email is required.");
        return;
      }
      if (!trimmed.includes("@") || !trimmed.endsWith(ALLOWED_DOMAIN)) {
        setEmailError("Only @blitzy.com email addresses are allowed.");
        return;
      }

      login(trimmed);
      navigate(nextDestination, { replace: true });
    },
    [email, login, navigate, nextDestination],
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

  return (
    <section
      className="min-h-screen flex items-center justify-center bg-slate-50 px-4 py-8"
      aria-labelledby="login-heading"
      data-testid="login-screen"
    >
      <div className="w-full max-w-md bg-white border border-slate-200 rounded-lg shadow-card p-6 sm:p-8">
        <div className="text-center mb-6">
          <h1 id="login-heading" className="text-2xl font-semibold text-slate-900">
            Sign in to Sales-Connections
          </h1>
          <p className="mt-2 text-sm text-slate-600">Enter your @blitzy.com email to continue.</p>
        </div>

        <form
          onSubmit={handleSubmit}
          noValidate
          data-testid="login-form"
        >
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

            <Button
              type="submit"
              variant="primary"
              size="md"
              fullWidth
              leftIcon={<LogIn aria-hidden="true" />}
              data-testid="login-submit"
            >
              Continue
            </Button>
          </div>
        </form>
      </div>
    </section>
  );
}
