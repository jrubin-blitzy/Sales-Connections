import { useCallback, useEffect, useState, type FormEvent, type JSX } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Lock, LogIn, Mail, User } from "lucide-react";

import { useRegisterMutation } from "@/api/auth";
import { useSession, useSessionLoading } from "@/auth/AuthProvider";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { useToast } from "@/components/ui/Toast";
import { RegisterRequestSchema, type RegisterRequest } from "@/schemas/auth";

export function RegisterScreen(): JSX.Element {
  const navigate = useNavigate();
  const session = useSession();
  const sessionLoading = useSessionLoading();
  const registerMutation = useRegisterMutation();
  const toast = useToast();

  const emailInputRef = useCallback((node: HTMLInputElement | null): void => {
    if (node !== null) node.focus();
  }, []);

  const [email, setEmail] = useState<string>("");
  const [displayName, setDisplayName] = useState<string>("");
  const [password, setPassword] = useState<string>("");
  const [confirmPassword, setConfirmPassword] = useState<string>("");
  const [emailError, setEmailError] = useState<string | undefined>(undefined);
  const [displayNameError, setDisplayNameError] = useState<string | undefined>(undefined);
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [confirmPasswordError, setConfirmPasswordError] = useState<string | undefined>(undefined);
  const [formError, setFormError] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (sessionLoading) return;
    if (session?.authenticated === true) {
      navigate("/feed", { replace: true });
    }
  }, [session, sessionLoading, navigate]);

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>): void => {
      event.preventDefault();

      setEmailError(undefined);
      setDisplayNameError(undefined);
      setPasswordError(undefined);
      setConfirmPasswordError(undefined);
      setFormError(undefined);

      const candidate: RegisterRequest = {
        email,
        display_name: displayName,
        password,
        confirm_password: confirmPassword,
      };

      const result = RegisterRequestSchema.safeParse(candidate);
      if (!result.success) {
        const fieldErrors = result.error.flatten().fieldErrors;
        if (fieldErrors.email?.[0]) setEmailError(fieldErrors.email[0]);
        if (fieldErrors.display_name?.[0]) setDisplayNameError(fieldErrors.display_name[0]);
        if (fieldErrors.password?.[0]) setPasswordError(fieldErrors.password[0]);
        if (fieldErrors.confirm_password?.[0])
          setConfirmPasswordError(fieldErrors.confirm_password[0]);
        return;
      }

      registerMutation.mutate(result.data, {
        onSuccess: () => {
          navigate("/feed", { replace: true });
        },
        onError: (error) => {
          if (error.status === 409) {
            setEmailError("An account with this email already exists.");
            return;
          }
          if (error.status === 422) {
            const emailFieldError = error.fieldError("email");
            const nameFieldError = error.fieldError("display_name");
            const passwordFieldError = error.fieldError("password");
            if (emailFieldError) setEmailError(emailFieldError.message ?? "Invalid email.");
            if (nameFieldError) setDisplayNameError(nameFieldError.message ?? "Invalid name.");
            if (passwordFieldError)
              setPasswordError(passwordFieldError.message ?? "Invalid password.");
            if (!emailFieldError && !nameFieldError && !passwordFieldError) {
              setFormError(error.message.length > 0 ? error.message : "Validation failed.");
            }
            return;
          }
          if (error.status === 0 || error.status >= 500) {
            setFormError("We could not reach the server. Please try again in a moment.");
            toast.error("Network error. Please check your connection.");
            return;
          }
          setFormError(
            error.message.length > 0
              ? error.message
              : "An unexpected error occurred. Please try again.",
          );
        },
      });
    },
    [email, displayName, password, confirmPassword, registerMutation, navigate, toast],
  );

  if (sessionLoading) {
    return (
      <section
        className="min-h-screen flex items-center justify-center bg-slate-50 px-4 py-8"
        aria-busy="true"
        data-testid="register-screen-loading"
      >
        <div className="text-sm text-slate-500">Loading...</div>
      </section>
    );
  }

  if (session?.authenticated === true) {
    return <section className="min-h-screen" aria-hidden="true" />;
  }

  const isSubmitting = registerMutation.isPending;

  return (
    <section
      className="min-h-screen flex items-center justify-center bg-slate-50 px-4 py-8"
      aria-labelledby="register-heading"
      data-testid="register-screen"
    >
      <div className="w-full max-w-md bg-white border border-slate-200 rounded-lg shadow-card p-6 sm:p-8">
        <div className="text-center mb-6">
          <h1 id="register-heading" className="text-2xl font-semibold text-slate-900">
            Create your account
          </h1>
          <p className="mt-2 text-sm text-slate-600">
            Join Sales-Connections and turn your network into leads.
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          noValidate
          aria-describedby={formError !== undefined ? "register-form-error" : undefined}
          data-testid="register-form"
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
              placeholder="you@company.com"
              disabled={isSubmitting}
              data-testid="register-email"
            />
            <Input
              label="Full name"
              type="text"
              name="display_name"
              autoComplete="name"
              required
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              errorMessage={displayNameError}
              leftIcon={<User aria-hidden="true" />}
              placeholder="Jane Smith"
              disabled={isSubmitting}
              data-testid="register-display-name"
            />
            <Input
              label="Password"
              type="password"
              name="password"
              autoComplete="new-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              errorMessage={passwordError}
              leftIcon={<Lock aria-hidden="true" />}
              placeholder="At least 8 characters"
              disabled={isSubmitting}
              data-testid="register-password"
            />
            <Input
              label="Confirm password"
              type="password"
              name="confirm_password"
              autoComplete="new-password"
              required
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              errorMessage={confirmPasswordError}
              leftIcon={<Lock aria-hidden="true" />}
              placeholder="Re-enter your password"
              disabled={isSubmitting}
              data-testid="register-confirm-password"
            />

            {formError !== undefined && (
              <p
                id="register-form-error"
                role="alert"
                className="text-sm text-red-600"
                data-testid="register-form-error"
              >
                {formError}
              </p>
            )}

            <Button
              type="submit"
              variant="primary"
              size="md"
              fullWidth
              loading={isSubmitting}
              leftIcon={<LogIn aria-hidden="true" />}
              data-testid="register-submit"
            >
              Create account
            </Button>
          </div>
        </form>

        <p className="mt-6 text-center text-sm text-slate-600">
          Already have an account?{" "}
          <Link to="/login" className="font-medium text-blue-600 hover:text-blue-500">
            Sign in
          </Link>
        </p>
      </div>
    </section>
  );
}
