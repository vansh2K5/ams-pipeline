// All copy on the page. Facts come from the real AMS repo and its example run.
export const REPO_URL = "https://github.com/vansh2K5/ams-pipeline";
export const REPORT_URL = "https://vansh2k5.github.io/ams-pipeline/";
export const INSTALL = "pip install git+https://github.com/vansh2K5/ams-pipeline";
export const RUN = "ams run --provider examples/order-service --consumer examples/billing-service";

export const hero = {
  eyebrow: "AMS · Automated Microservice Synthesis",
  title: ["Two services.", "One contract."],
  lede:
    "AMS reads a Spring Boot provider and a FastAPI consumer, works out which fields mean the same thing, writes the integration layer between them, and proves it works.",
  hint: "Touch the water",
};

// Story beats, pinned over the liquid while it splits, exchanges and merges.
// `at` is the story progress (0..1) where the beat is centred.
export const beats = [
  {
    at: 0.12,
    kicker: "01 · Silence",
    title: "Same data. Different words.",
    body: "Java's Order says customerId, totalAmount, createdAt. Python's OrderRecord expects client_id, amount_due, placed_at. Nothing connects.",
  },
  {
    at: 0.32,
    kicker: "02 · Structure",
    title: "Parsers read exact structure.",
    body: "tree-sitter for Java, the ast module for Python, one normalized schema. Deterministic: a parser never invents a field.",
  },
  {
    at: 0.56,
    kicker: "03 · Meaning",
    title: "Meaning crosses the gap.",
    body: "An LLM (Gemini, Groq or Claude) proposes which fields are the same thing. Every proposal is checked against the parsed schema; invented fields and type clashes are rejected. Tap a droplet.",
  },
  {
    at: 0.86,
    kicker: "04 · Union",
    title: "One contract, generated.",
    body: "Typed DTOs with wire aliases, a mapper, a pooled HTTP client with retries, a hardened docker-compose and a fully pinned dependency tree.",
  },
];

export const proof = {
  kicker: "05 · Proof",
  title: "Compiling isn't the same as correct.",
  body: "A self-healing loop fixes what breaks the build: Ruff first, then an LLM repair with the exact errors. But a repair that compiles can still be wrong, so AMS then checks every value end to end against a mock of the provider built from its own schema.",
  stats: [
    { value: "14/14", label: "field checks, end to end" },
    { value: "7/7", label: "fields mapped" },
    { value: "5", label: "scanners in parallel" },
    { value: "0", label: "known-vulnerable deps" },
  ],
  scanners: ["Semgrep", "Gitleaks", "OSV-Scanner", "Trivy", "Checkov"],
  scanNote: "Skipped is never clean: a scanner that examined nothing is reported as skipped, with the reason.",
};

export const providers = [
  { name: "Gemini", model: "gemini-flash-latest", key: "GEMINI_API_KEY" },
  { name: "Groq", model: "openai/gpt-oss-120b", key: "GROQ_API_KEY" },
  { name: "Claude", model: "claude-sonnet-5-5", key: "ANTHROPIC_API_KEY" },
];
