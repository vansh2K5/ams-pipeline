"use client";

import { useEffect, useRef, useState } from "react";

/*
 * The liquid yin-yang: Blender-rendered footage drawn through WebGL2.
 *  - At the top of the page the seamless idle loop plays like a video.
 *  - Scrolling through `storyRef` scrubs the story clip (split, field droplets crossing, merge).
 *  - The pointer ripples the liquid: a CPU height-field sim uploaded as a half-float texture
 *    refracts the frame and adds glints, masked to the liquid so the eggshell stays calm.
 *  - Live HTML labels ride the droplets (positions exported per frame from Blender).
 */

type Seq = { name: string; count: number; frames: HTMLImageElement[] };
type Story = {
  fps: number;
  frames: number;
  droplets: { provider: string; consumer: string; confidence: number; switch: number }[];
  positions: [number, number, number][][];
};

const FRAME_ASPECT = 16 / 9;
const EGGSHELL = [0.953, 0.945, 0.925];
const SIM_W = 220;
const SIM_H = 124;

const VERT = `#version 300 es
in vec2 aPos; out vec2 vUv;
void main() { vUv = aPos * 0.5 + 0.5; vUv.y = 1.0 - vUv.y; gl_Position = vec4(aPos, 0.0, 1.0); }`;

const FRAG = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 outColor;
uniform sampler2D uA, uB, uRipple;
uniform float uMix;
uniform vec2 uScale, uTexel;
uniform vec3 uEgg;
vec3 frame(vec2 uv) {
  if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) return uEgg;
  return mix(texture(uA, uv).rgb, texture(uB, uv).rgb, uMix);
}
void main() {
  vec2 fuv = (vUv - 0.5) * uScale + 0.5;
  float hx = texture(uRipple, vUv + vec2(uTexel.x, 0.0)).r - texture(uRipple, vUv - vec2(uTexel.x, 0.0)).r;
  float hy = texture(uRipple, vUv + vec2(0.0, uTexel.y)).r - texture(uRipple, vUv - vec2(0.0, uTexel.y)).r;
  vec2 grad = vec2(hx, hy);
  vec3 base = frame(fuv);
  float liquid = smoothstep(0.05, 0.16, distance(base, uEgg));
  vec3 col = frame(fuv + grad * 0.018 * liquid);
  vec3 n = normalize(vec3(-grad * 9.0, 1.0));
  float glint = pow(max(dot(n, normalize(vec3(-0.45, -0.55, 1.0))), 0.0), 90.0);
  float lit = clamp(length(grad) * 40.0, 0.0, 1.0);
  col += glint * lit * liquid * 0.55;
  outColor = vec4(col, 1.0);
}`;

function compile(gl: WebGL2RenderingContext, type: number, src: string) {
  const s = gl.createShader(type)!;
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader");
  return s;
}

function texture(gl: WebGL2RenderingContext) {
  const t = gl.createTexture()!;
  gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return t;
}

async function loadSeq(name: string): Promise<Seq> {
  const m = await fetch(`/liquid/seq/${name}/manifest.json`).then((r) => r.json());
  const frames = Array.from({ length: m.count }, (_, i) => {
    const img = new Image();
    img.decoding = "async";
    img.src = `/liquid/seq/${name}/${String(i).padStart(4, "0")}.${m.ext}`;
    return img;
  });
  return { name, count: m.count, frames };
}

/** Screen-space mapping that covers the viewport with the 16:9 footage. */
function coverScale(w: number, h: number): [number, number] {
  const sa = w / h;
  return sa > FRAME_ASPECT ? [1, FRAME_ASPECT / sa] : [sa / FRAME_ASPECT, 1];
}

export default function LiquidStage({ storyRef }: { storyRef: React.RefObject<HTMLElement | null> }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const labelsRef = useRef<HTMLDivElement>(null);
  const [story, setStory] = useState<Story | null>(null);
  const [loaded, setLoaded] = useState(0);
  const [card, setCard] = useState<number | null>(null);

  useEffect(() => {
    fetch("/liquid/story.json").then((r) => r.json()).then(setStory).catch(() => setStory(null));
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current!;
    const gl = canvas.getContext("webgl2", { antialias: false, premultipliedAlpha: false });
    if (!gl) return;
    let raf = 0;
    let disposed = false;
    let seqs: { idle?: Seq; story?: Seq } = {};

    (async () => {
      const index: { idle?: string; story?: string } = await fetch("/liquid/seq/index.json")
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({}));
      const [idle, st] = await Promise.all([
        index.idle ? loadSeq(index.idle) : undefined,
        index.story ? loadSeq(index.story) : undefined,
      ]);
      seqs = { idle: idle ?? st, story: st ?? idle };
      const all = [...(idle?.frames ?? []), ...(st?.frames ?? [])];
      let done = 0;
      all.forEach((img) => img.decode().catch(() => {}).finally(() => !disposed && setLoaded(++done / all.length)));
    })();

    const prog = gl.createProgram()!;
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const aPos = gl.getAttribLocation(prog, "aPos");
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
    const u = (n: string) => gl.getUniformLocation(prog, n);

    const texA = texture(gl), texB = texture(gl), texR = texture(gl);
    [texA, texB].forEach((t) => {
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, 1, 1, 0, gl.RGB, gl.UNSIGNED_BYTE, new Uint8Array([243, 241, 236]));
    });
    gl.uniform1i(u("uA"), 0);
    gl.uniform1i(u("uB"), 1);
    gl.uniform1i(u("uRipple"), 2);
    gl.uniform3fv(u("uEgg"), EGGSHELL);
    gl.uniform2f(u("uTexel"), 1 / SIM_W, 1 / SIM_H);

    // ripple height field (two buffers, classic wave propagation)
    let cur = new Float32Array(SIM_W * SIM_H), prev = new Float32Array(SIM_W * SIM_H);
    const poke = (x: number, y: number, amount: number, radius = 3) => {
      const cx = Math.round(x * (SIM_W - 1)), cy = Math.round(y * (SIM_H - 1));
      for (let j = -radius; j <= radius; j++)
        for (let i = -radius; i <= radius; i++) {
          const px = cx + i, py = cy + j;
          if (px < 1 || py < 1 || px >= SIM_W - 1 || py >= SIM_H - 1) continue;
          const fall = Math.max(0, 1 - Math.hypot(i, j) / (radius + 0.5));
          cur[py * SIM_W + px] += amount * fall;
        }
    };
    let last: [number, number] | null = null;
    const onMove = (e: PointerEvent) => {
      const x = e.clientX / innerWidth, y = e.clientY / innerHeight;
      const speed = last ? Math.min(1, Math.hypot(x - last[0], y - last[1]) * 30) : 0.2;
      poke(x, y, 0.12 + speed * 0.5);
      last = [x, y];
    };
    const onDown = (e: PointerEvent) => poke(e.clientX / innerWidth, e.clientY / innerHeight, 1.6, 6);
    addEventListener("pointermove", onMove);
    addEventListener("pointerdown", onDown);

    const resize = () => {
      const dpr = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.round(innerWidth * dpr);
      canvas.height = Math.round(innerHeight * dpr);
      gl.viewport(0, 0, canvas.width, canvas.height);
    };
    resize();
    addEventListener("resize", resize);

    const shown = { a: -1, b: -1 };
    const upload = (tex: WebGLTexture, unit: number, img: HTMLImageElement | undefined) => {
      if (!img || !img.complete || !img.naturalWidth) return false;
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img);
      return true;
    };

    const t0 = performance.now();
    // ?story=0.5 pins the story progress (for screenshots and review)
    const pinned = new URLSearchParams(location.search).get("story");
    const tick = () => {
      raf = requestAnimationFrame(tick);
      const el = storyRef.current;
      let p = 0;
      if (pinned !== null) {
        p = Math.min(1, Math.max(0, Number(pinned)));
      } else if (el) {
        const r = el.getBoundingClientRect();
        p = Math.min(1, Math.max(0, -r.top / Math.max(1, r.height - innerHeight)));
      }
      const { idle, story: st } = seqs;
      if (idle) {
        const i = Math.floor(((performance.now() - t0) / 1000) * 24) % idle.count;
        if (i !== shown.a && upload(texA, 0, idle.frames[i])) shown.a = i;
      }
      let storyFrame = 0;
      if (st) {
        storyFrame = Math.round(p * (st.count - 1));
        if (storyFrame !== shown.b && upload(texB, 1, st.frames[storyFrame])) shown.b = storyFrame;
      }
      // idle until the first scroll, then the scrubbed story takes over
      const mix = Math.min(1, Math.max(0, p / 0.035));

      // ripple step
      const next = prev;
      for (let y = 1; y < SIM_H - 1; y++)
        for (let x = 1; x < SIM_W - 1; x++) {
          const k = y * SIM_W + x;
          next[k] = ((cur[k - 1] + cur[k + 1] + cur[k - SIM_W] + cur[k + SIM_W]) * 0.5 - next[k]) * 0.982;
        }
      prev = cur;
      cur = next;
      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, texR);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, SIM_W, SIM_H, 0, gl.RED, gl.FLOAT, cur);

      const [sx, sy] = coverScale(innerWidth, innerHeight);
      gl.uniform2f(u("uScale"), sx, sy);
      gl.uniform1f(u("uMix"), mix);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texA);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, texB);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      // labels follow the droplets of the current story frame
      const box = labelsRef.current;
      if (box && storyData.current && st) {
        const sd = storyData.current;
        const f = Math.min(sd.frames - 1, Math.round((storyFrame / Math.max(1, st.count - 1)) * (sd.frames - 1)));
        const show = mix > 0.9 && f > 30 && f < sd.frames - 14;
        box.style.opacity = show ? "1" : "0";
        sd.positions[f].forEach(([fx, fy, phase], i) => {
          const lab = box.children[i] as HTMLElement | undefined;
          if (!lab) return;
          const x = ((fx - 0.5) / sx + 0.5) * innerWidth, y = ((fy - 0.5) / sy + 0.5) * innerHeight;
          lab.style.transform = `translate(${x}px, ${y}px)`;
          lab.dataset.phase = String(phase);
        });
      }
    };
    raf = requestAnimationFrame(tick);
    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      removeEventListener("pointermove", onMove);
      removeEventListener("pointerdown", onDown);
      removeEventListener("resize", resize);
    };
  }, [storyRef]);

  const storyData = useRef<Story | null>(null);
  storyData.current = story;
  const active = card !== null && story ? story.droplets[card] : null;

  return (
    <div className="stage" aria-hidden={false}>
      <canvas ref={canvasRef} className="stage-canvas" aria-hidden="true" />
      <div ref={labelsRef} className="labels">
        {story?.droplets.map((d, i) => (
          <button key={d.provider} className="label" onClick={() => setCard(i)} data-phase="1">
            <span className="label-provider">{d.provider}</span>
            <span className="label-consumer">{d.consumer}</span>
          </button>
        ))}
      </div>
      {loaded < 0.9 && (
        <div className="loading" aria-live="polite">
          <span>AMS</span>
          <b>{Math.round(loaded * 100)}%</b>
        </div>
      )}
      {active && (
        <div className="card" role="dialog" aria-label={`${active.provider} maps to ${active.consumer}`}>
          <button className="card-close" onClick={() => setCard(null)} aria-label="Close">×</button>
          <p className="card-kicker">Field mapping</p>
          <p className="card-row"><span className="tag tag-java">Java · Order</span><code>{active.provider}</code></p>
          <p className="card-arrow">↓</p>
          <p className="card-row"><span className="tag tag-py">Python · OrderRecord</span><code>{active.consumer}</code></p>
          <div className="card-conf">
            <div className="meter"><i style={{ width: `${active.confidence * 100}%` }} /></div>
            <span>{active.confidence.toFixed(2)} confidence · one recorded Groq run, validated against the parsed schema</span>
          </div>
        </div>
      )}
    </div>
  );
}
