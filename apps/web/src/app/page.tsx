export default function HomePage() {
  return (
    <main className="shell">
      <div className="eyebrow">NASTYA / MVP</div>
      <h1>Talk naturally, across languages.</h1>
      <p>
        A private Vietnamese ↔ Russian video-call translator. The room and AI
        pipeline are coming in the next milestones.
      </p>
      <div className="status" role="status">
        <span className="dot" aria-hidden="true" />
        Scaffold ready · Calling not connected
      </div>
      <a href="/api/health">Application health</a>
    </main>
  );
}
