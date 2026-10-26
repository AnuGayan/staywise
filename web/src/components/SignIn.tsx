export interface SignInProps {
  loginUrl: string;
}

const HIGHLIGHTS = [
  {
    id: "auto",
    title: "Automatic step-up",
    body: "The agent quietly exchanges tokens for narrower, task-specific scopes.",
  },
  {
    id: "mfa",
    title: "User step-up",
    body: "Sensitive actions like payments pause for your MFA approval.",
  },
  {
    id: "audit",
    title: "Full transparency",
    body: "Every token, scope and delegation is shown on a live security timeline.",
  },
];

export function SignIn({ loginUrl }: SignInProps) {
  return (
    <main className="signin">
      <div className="signin-card">
        <span className="brand-mark brand-mark-lg" aria-hidden="true">
          SW
        </span>
        <h1>StayWise Concierge</h1>
        <p className="signin-lead">
          Your AI hotel concierge — search, book, and pay with security that steps up only when it
          needs to.
        </p>
        <a className="btn btn-primary btn-lg" href={loginUrl}>
          Sign in to start
        </a>
        <ul className="signin-highlights">
          {HIGHLIGHTS.map((h) => (
            <li key={h.id}>
              <strong>{h.title}</strong>
              <span>{h.body}</span>
            </li>
          ))}
        </ul>
      </div>
    </main>
  );
}
