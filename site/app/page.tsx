"use client";

import Lenis from "lenis";
import { useEffect, useRef, useState } from "react";

import LiquidStage from "@/components/LiquidStage";
import { INSTALL, REPO_URL, REPORT_URL, RUN, beats, hero, proof, providers } from "@/lib/content";

function CopyLine({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="copy-line">
      <code>{text}</code>
      <button
        onClick={() => navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1400);
        })}
        aria-live="polite"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export default function Page() {
  const storyRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const lenis = new Lenis({ lerp: 0.085, smoothWheel: true });
    let raf = 0;
    const loop = (t: number) => {
      lenis.raf(t);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    // reveal copy blocks as they enter the viewport
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => e.target.classList.toggle("in", e.isIntersecting)),
      { threshold: 0.35 },
    );
    document.querySelectorAll(".reveal").forEach((el) => io.observe(el));
    return () => {
      cancelAnimationFrame(raf);
      lenis.destroy();
      io.disconnect();
    };
  }, []);

  return (
    <main>
      <a className="skip" href="#run">Skip to install</a>
      <header className="nav">
        <span className="brand">AMS</span>
        <nav>
          <a href={REPO_URL}>GitHub</a>
          <a href={REPORT_URL}>Live report</a>
        </nav>
      </header>

      {/* fixed behind the hero and the story; later sections slide over it */}
      <LiquidStage storyRef={storyRef} />

      <section className="hero">
        <h1 className="hero-title">
          <span className="reveal in">{hero.title[0]}</span>
          <em className="reveal in">{hero.title[1]}</em>
        </h1>
        <div className="hero-copy reveal in">
          <p className="eyebrow">{hero.eyebrow}</p>
          <p className="lede">{hero.lede}</p>
          <div className="ctas">
            <a className="btn btn-ink" href={REPO_URL}>View on GitHub</a>
            <a className="btn" href={REPORT_URL}>Live CI report</a>
          </div>
        </div>
        <p className="hint">{hero.hint}</p>
      </section>

      {/* this tall section scrubs the story clip as it scrolls past */}
      <section ref={storyRef} className="story" aria-label="How AMS works">
        {beats.map((b, i) => (
          <article key={b.kicker} className={`beat reveal ${i % 2 ? "beat-right" : ""}`} style={{ top: `${b.at * 100}%` }}>
            <p className="kicker">{b.kicker}</p>
            <h2>{b.title}</h2>
            <p>{b.body}</p>
          </article>
        ))}
      </section>

      <section className="proof">
        <div className="wrap reveal">
          <p className="kicker">{proof.kicker}</p>
          <h2>{proof.title}</h2>
          <p className="lede">{proof.body}</p>
          <div className="stats">
            {proof.stats.map((s) => (
              <div key={s.label} className="stat">
                <b>{s.value}</b>
                <span>{s.label}</span>
              </div>
            ))}
          </div>
          <ul className="scanners">
            {proof.scanners.map((s) => <li key={s}>{s}</li>)}
          </ul>
          <p className="note">{proof.scanNote}</p>
        </div>
      </section>

      <section id="run" className="run">
        <div className="wrap reveal">
          <p className="kicker">06 · Run it</p>
          <h2>Open source. <em>Point it at two services.</em></h2>
          <CopyLine text={INSTALL} />
          <CopyLine text={RUN} />
          <table className="providers">
            <caption>Bring any one LLM key, or run fully deterministic with --no-llm</caption>
            <thead><tr><th>Provider</th><th>Default model</th><th>Key</th></tr></thead>
            <tbody>
              {providers.map((p) => (
                <tr key={p.name}><td>{p.name}</td><td><code>{p.model}</code></td><td><code>{p.key}</code></td></tr>
              ))}
            </tbody>
          </table>
          <div className="ctas">
            <a className="btn btn-ink" href={REPO_URL}>GitHub · MIT</a>
            <a className="btn" href={REPORT_URL}>Live CI report</a>
          </div>
        </div>
      </section>

      <footer className="foot">
        <span>AMS · Automated Microservice Synthesis</span>
        <span>Liquid rendered in Blender Cycles</span>
      </footer>
    </main>
  );
}
