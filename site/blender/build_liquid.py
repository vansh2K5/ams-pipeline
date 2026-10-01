"""Liquid yin-yang hero for the AMS landing page, matched to `UI ref/` (Blender 5.x, Cycles).

A domed disc of zero-gravity liquid: indigo water on the left, amber on the right, swirl
ripples, a frothy iridescent S-seam of bubbles, vortex eyes with splash crowns, stray
droplets, on a flat eggshell background. Art-directed geometry + procedural shading, not a
fluid sim, so it is controllable and repeatable.

    "F:/Blender/blender.exe" -b --factory-startup --python site/blender/build_liquid.py -- [--fast] [--res 3840]

--fast renders 1280x720 at low samples for look-dev; the default is 3840x2160.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import bpy
from mathutils import Vector

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
FAST = "--fast" in ARGS
RES_X = int(ARGS[ARGS.index("--res") + 1]) if "--res" in ARGS else (1280 if FAST else 3840)
RES_Y = RES_X * 9 // 16
SAMPLES = int(ARGS[ARGS.index("--samples") + 1]) if "--samples" in ARGS else (96 if FAST else 1024)
OUT = Path(__file__).resolve().parent.parent / "public" / "liquid"
FPS = 24
SPLIT = float(ARGS[ARGS.index("--split") + 1]) if "--split" in ARGS else 0.0  # test pose: 0 joined .. 1 apart
OUT.mkdir(parents=True, exist_ok=True)

R = 1.0  # disc radius
DOME = 0.30  # half-thickness of the lens at its centre
EYE_R = 0.17
EGGSHELL = (0.953, 0.945, 0.925)
INDIGO = (0.035, 0.055, 0.27)
INDIGO_DEEP = (0.008, 0.01, 0.06)
VIOLET = (0.20, 0.08, 0.38)
AMBER = (1.0, 0.42, 0.04)
GOLD = (1.0, 0.70, 0.22)
rng = random.Random(7)


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lin(rgb):
    return (*[srgb_to_linear(c) for c in rgb], 1.0)


# ------------------------------------------------------------------------------- scene setup
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.render.resolution_x, scene.render.resolution_y = RES_X, RES_Y
scene.render.film_transparent = False
scene.view_settings.view_transform = "Standard"  # keeps the eggshell background exact
scene.view_settings.look = "None"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
scene.cycles.max_bounces = 32
scene.cycles.transmission_bounces = 32
scene.cycles.transparent_max_bounces = 32
scene.cycles.volume_bounces = 2
scene.cycles.glossy_bounces = 8
scene.cycles.caustics_reflective = False
scene.cycles.caustics_refractive = False
scene.cycles.blur_glossy = 0.5
scene.cycles.volume_step_rate = 0.5 if not FAST else 1.0
scene.render.use_persistent_data = True  # reuse geometry/BVH between animation frames

prefs = bpy.context.preferences.addons["cycles"].preferences
for backend in ("OPTIX", "CUDA"):
    try:
        prefs.compute_device_type = backend
        prefs.get_devices()
        gpus = [d for d in prefs.devices if d.type == backend]
        if gpus:
            for d in prefs.devices:
                d.use = d.type == backend
            scene.cycles.device = "GPU"
            print(f"[liquid] rendering on {backend}: {[d.name for d in gpus]}")
            break
    except TypeError:
        continue


# --------------------------------------------------------------------------- node helpers
class NodeBuilder:
    def __init__(self, tree):
        self.tree, self.nodes, self.links = tree, tree.nodes, tree.links
        self.x = 0

    def node(self, kind, **props):
        n = self.nodes.new(kind)
        n.location = (self.x, 0)
        self.x += 40
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, out, inp):
        self.links.new(out, inp)

    def _in(self, sock, v):
        if isinstance(v, bpy.types.NodeSocket):
            self.link(v, sock)
        else:
            sock.default_value = v

    def math(self, op, a, b=None, clamp=False):
        n = self.node("ShaderNodeMath", operation=op, use_clamp=clamp)
        self._in(n.inputs[0], a)
        if b is not None:
            self._in(n.inputs[1], b)
        return n.outputs[0]

    def vmath(self, op, a, b=None, scale=None):
        n = self.node("ShaderNodeVectorMath", operation=op)
        self._in(n.inputs[0], a)
        if b is not None:
            self._in(n.inputs[1], b)
        if scale is not None:
            n.inputs["Scale"].default_value = scale
        return n.outputs["Value"] if op in ("LENGTH", "DOT_PRODUCT", "DISTANCE") else n.outputs["Vector"]

    def separate(self, v):
        n = self.node("ShaderNodeSeparateXYZ")
        self._in(n.inputs[0], v)
        return n.outputs

    def rotate_z(self, vec, center, angle):
        n = self.node("ShaderNodeVectorRotate", rotation_type="Z_AXIS")
        self._in(n.inputs["Vector"], vec)
        n.inputs["Center"].default_value = center
        self._in(n.inputs["Angle"], angle)
        return n.outputs[0]

    def noise(self, vec, scale, detail=4.0, rough=0.55, distortion=0.0):
        n = self.node("ShaderNodeTexNoise", noise_dimensions="3D")
        self._in(n.inputs["Vector"], vec)
        n.inputs["Scale"].default_value = scale
        n.inputs["Detail"].default_value = detail
        n.inputs["Roughness"].default_value = rough
        n.inputs["Distortion"].default_value = distortion
        return n.outputs

    def wave_rings(self, vec, scale, distortion, detail=3.0):
        n = self.node("ShaderNodeTexWave", wave_type="RINGS", rings_direction="SPHERICAL")
        self._in(n.inputs["Vector"], vec)
        n.inputs["Scale"].default_value = scale
        n.inputs["Distortion"].default_value = distortion
        n.inputs["Detail"].default_value = detail
        return n.outputs["Fac"]

    def smooth_inside(self, d, width=0.008):
        """1 inside (d < 0), 0 outside, with a soft edge of `width`."""
        n = self.node("ShaderNodeMapRange", interpolation_type="SMOOTHSTEP")
        self._in(n.inputs["Value"], d)
        n.inputs["From Min"].default_value = width
        n.inputs["From Max"].default_value = -width
        return n.outputs["Result"]

    def mix_color(self, fac, a, b):
        n = self.node("ShaderNodeMix", data_type="RGBA")
        self._in(n.inputs["Factor"], fac)
        self._in(n.inputs[6], a)
        self._in(n.inputs[7], b)
        return n.outputs[2]


def time_socket(nb: NodeBuilder):
    """Seconds since frame 1, driven by the frame number, so every swirl flows in animation."""
    n = nb.node("ShaderNodeValue")
    n.label = "Time"
    fc = n.outputs[0].driver_add("default_value")
    fc.driver.expression = f"(frame - 1) / {FPS}"
    return n.outputs[0]


def swirled_coords(nb: NodeBuilder, tc, spiral, vortex, warp_amount, t=None, flow=1.0):
    """Object-space coordinates twisted into swirls: a global spiral (strongest at the centre)
    plus a vortex around each eye, with a little domain warp so nothing looks procedural-clean.
    The shape uses a gentle version (keeps the yin-yang readable); the colour streaks a strong one.
    With `t`, the spiral slowly tightens, the eye vortices keep turning and the warp evolves."""
    r = nb.vmath("LENGTH", tc)
    inner = nb.math("SUBTRACT", 1.0, r, clamp=True)
    spin = spiral if t is None else nb.math("ADD", spiral, nb.math("MULTIPLY", t, 0.06 * flow))
    p = nb.rotate_z(tc, (0, 0, 0), nb.math("MULTIPLY", inner, spin))
    eye_spin = vortex if t is None else nb.math("ADD", vortex, nb.math("MULTIPLY", t, 1.3 * flow))
    for cy in (R / 2, -R / 2):
        d = nb.vmath("DISTANCE", p, (0.0, cy, 0.0))
        a = nb.math("MULTIPLY", nb.math("EXPONENT", nb.math("MULTIPLY", d, -11.0)), eye_spin)
        p = nb.rotate_z(p, (0.0, cy, 0.0), a)
    n = nb.node("ShaderNodeTexNoise", noise_dimensions="4D")
    nb.link(p, n.inputs["Vector"])
    if t is not None:
        nb.link(nb.math("MULTIPLY", t, 0.1 * flow), n.inputs["W"])
    n.inputs["Scale"].default_value = 2.0
    n.inputs["Detail"].default_value = 3.0
    warp = nb.vmath("SUBTRACT", n.outputs["Color"], (0.5, 0.5, 0.5))
    return nb.vmath("ADD", p, nb.vmath("SCALE", warp, scale=warp_amount))


def taijitu_mask(nb: NodeBuilder, p):
    """1 = amber, 0 = indigo. Amber is the right half plus the top small circle, minus the bottom
    small circle; the eyes swap colour (indigo eye in the amber, gold eye in the indigo)."""
    x, _, _ = nb.separate(p)
    left = nb.smooth_inside(x)  # 1 where x < 0
    d_top = nb.math("SUBTRACT", nb.vmath("DISTANCE", p, (0.0, R / 2, 0.0)), R / 2)
    d_bot = nb.math("SUBTRACT", nb.vmath("DISTANCE", p, (0.0, -R / 2, 0.0)), R / 2)
    right = nb.math("SUBTRACT", 1.0, left)
    top, bot = nb.smooth_inside(d_top), nb.smooth_inside(d_bot)
    amber = nb.math("MULTIPLY", nb.math("MAXIMUM", right, top), nb.math("SUBTRACT", 1.0, bot), clamp=True)

    # eyes: splashy, swirling edges instead of clean circles
    edge = nb.math("MULTIPLY", nb.math("SUBTRACT", nb.noise(p, 9.0, detail=4.0)["Fac"], 0.5), 0.07)
    eyes = []
    for cy in (R / 2, -R / 2):
        swirl = nb.rotate_z(p, (0.0, cy, 0.0), nb.math("MULTIPLY", nb.math("EXPONENT", nb.math(
            "MULTIPLY", nb.vmath("DISTANCE", p, (0.0, cy, 0.0)), -14.0)), 4.0))
        d = nb.math("SUBTRACT", nb.vmath("DISTANCE", swirl, (0.0, cy, 0.0)), EYE_R)
        d = nb.math("ADD", d, nb.math("MULTIPLY", nb.math("SUBTRACT", nb.noise(swirl, 14.0, detail=3.0)["Fac"],
                                                            0.5), 0.06))
        eyes.append(nb.smooth_inside(nb.math("ADD", d, edge), 0.01))
    amber = nb.math("MULTIPLY", amber, nb.math("SUBTRACT", 1.0, eyes[0]), clamp=True)
    amber = nb.math("MAXIMUM", amber, eyes[1], clamp=True)

    # seam: distance to the S-curve (top circle on the left, bottom circle on the right)
    seam_d = nb.math("ADD", nb.math("MULTIPLY", left, nb.math("ABSOLUTE", d_top)),
                     nb.math("MULTIPLY", right, nb.math("ABSOLUTE", d_bot)))
    seam = nb.smooth_inside(nb.math("SUBTRACT", seam_d, 0.035), 0.03)
    return amber, seam, eyes


# ------------------------------------------------------------------------------ materials
def liquid_material():
    mat = bpy.data.materials.new("Liquid")
    # bump only: the swirl shading is animated, and true displacement would force Cycles to
    # re-tessellate the disc every frame. The rim silhouette is baked into the mesh instead.
    mat.displacement_method = "BUMP"
    nb = NodeBuilder(mat.node_tree)
    nb.nodes.clear()
    out = nb.node("ShaderNodeOutputMaterial")

    tc3 = nb.node("ShaderNodeTexCoord").outputs["Object"]
    # flatten to the disc plane: the dome puts surface points up to DOME above z=0, which would
    # push every 3D distance to an eye centre past the eye radius
    tc = nb.vmath("MULTIPLY", tc3, (1.0, 1.0, 0.0))
    t = time_socket(nb)
    # the colour boundary must sit exactly on the boolean cut, or wrong-colour slivers show when the
    # halves part; the organic look of the seam comes from the froth and the seam band instead
    p_shape = swirled_coords(nb, tc, spiral=0.0, vortex=0.0, warp_amount=0.004, t=t, flow=0.0)
    p = swirled_coords(nb, tc, spiral=1.6, vortex=7.0, warp_amount=0.05, t=t)
    amber, seam, eyes = taijitu_mask(nb, p_shape)
    r = nb.vmath("LENGTH", tc)

    # colour: long swirl streaks (stretched noise in the swirled space) + soft rings
    streak = nb.noise(nb.vmath("MULTIPLY", p, (1.0, 4.0, 1.0)), 2.6, detail=7.0, rough=0.6)["Fac"]
    streak2 = nb.noise(nb.vmath("MULTIPLY", p, (4.0, 1.0, 1.0)), 3.4, detail=5.0, rough=0.55)["Fac"]
    streak = nb.math("POWER", streak, 1.6)  # sharpen into brush-stroke-like swirl lines
    streak2 = nb.math("POWER", streak2, 1.4)
    rings = nb.wave_rings(p, 2.4, 5.0)
    pattern = nb.math("ADD", nb.math("MULTIPLY", streak, 0.55), nb.math("MULTIPLY", rings, 0.25))
    pattern = nb.math("ADD", pattern, nb.math("MULTIPLY", streak2, 0.2))
    rim = nb.math("POWER", r, 5.0)  # deeper, richer towards the edge
    hi = nb.math("POWER", streak, 5.0)  # sparse bright swirl highlights

    indigo_col = nb.mix_color(pattern, lin(INDIGO_DEEP), lin(INDIGO))
    indigo_col = nb.mix_color(nb.math("MULTIPLY", nb.math("POWER", streak2, 3.0), 0.9), indigo_col, lin(VIOLET))
    indigo_col = nb.mix_color(nb.math("MULTIPLY", hi, 2.5), indigo_col, lin((0.25, 0.42, 0.95)))
    amber_col = nb.mix_color(pattern, lin((0.86, 0.38, 0.04)), lin((0.99, 0.62, 0.22)))
    amber_col = nb.mix_color(nb.math("MULTIPLY", hi, 2.0), amber_col, lin((1.0, 0.86, 0.62)))
    amber_col = nb.mix_color(nb.math("MULTIPLY", rim, 0.9), amber_col, lin((0.62, 0.22, 0.02)))
    colour = nb.mix_color(amber, indigo_col, amber_col)
    # eyes: bright spiral swirls (cream in the gold eye, pale blue in the indigo eye)
    for cy, eye, tint in ((R / 2, eyes[0], (0.22, 0.42, 0.92)), (-R / 2, eyes[1], (1.0, 0.82, 0.48))):
        local = nb.rotate_z(tc, (0.0, cy, 0.0), nb.math("MULTIPLY", nb.math("EXPONENT", nb.math(
            "MULTIPLY", nb.vmath("DISTANCE", tc, (0.0, cy, 0.0)), -12.0)), 14.0))
        # straight bands twisted by a radius-dependent vortex = a real spiral (rings would stay rings)
        bands = nb.node("ShaderNodeTexWave", wave_type="BANDS", bands_direction="X")
        nb.link(nb.vmath("SUBTRACT", local, (0.0, cy, 0.0)), bands.inputs["Vector"])
        bands.inputs["Scale"].default_value = 9.0
        bands.inputs["Distortion"].default_value = 2.5
        bands.inputs["Detail"].default_value = 2.0
        spiral = nb.math("POWER", bands.outputs["Fac"], 3.0)
        core = nb.math("EXPONENT", nb.math("MULTIPLY", nb.math("POWER", nb.vmath("DISTANCE", tc, (0.0, cy, 0.0)),
                                                                  2.0), -120.0))
        glow = nb.math("MULTIPLY", eye, nb.math("ADD", nb.math("MULTIPLY", spiral, 0.5), nb.math("MULTIPLY", core, 0.4)),
                       clamp=True)
        colour = nb.mix_color(glow, colour, lin(tint))
    # the seam crest is mixed liquid: deep teal-violet under the bubbles
    seam_col = nb.mix_color(streak2, lin((0.04, 0.22, 0.30)), lin((0.18, 0.08, 0.32)))
    colour = nb.mix_color(nb.math("MULTIPLY", seam, 0.45), colour, seam_col)

    # surface: thick, saturated, wet liquid: coloured base under a glassy clear-coat
    surf = nb.node("ShaderNodeBsdfPrincipled")
    nb.link(colour, surf.inputs["Base Color"])
    surf.inputs["Roughness"].default_value = 0.22
    surf.inputs["IOR"].default_value = 1.33
    surf.inputs["Transmission Weight"].default_value = 0.08
    surf.inputs["Subsurface Weight"].default_value = 0.35
    surf.inputs["Subsurface Radius"].default_value = (0.5, 0.25, 0.12)
    surf.inputs["Coat Weight"].default_value = 1.0
    surf.inputs["Coat Roughness"].default_value = 0.06
    surf.inputs["Coat IOR"].default_value = 1.38
    nb.link(surf.outputs[0], out.inputs["Surface"])

    # displacement: broad swirl ripples, vortex dips at the eyes, a raised crest along the seam,
    # and a ragged, splashy rim (displacing along the edge normals breaks the perfect circle)
    ripple = nb.wave_rings(p, 4.0, 3.0, detail=2.0)
    fine = nb.noise(p, 12.0, detail=4.0)["Fac"]
    height = nb.math("ADD", nb.math("MULTIPLY", ripple, 0.0025), nb.math("MULTIPLY", fine, 0.0025))
    height = nb.math("ADD", height, nb.math("MULTIPLY", seam, 0.022))
    for cy, eye in zip((R / 2, -R / 2), eyes):
        height = nb.math("ADD", height, nb.math("MULTIPLY", eye, 0.0))  # no dip: a concave eye mirrors the lights as stripes
    disp = nb.node("ShaderNodeDisplacement")
    nb.link(height, disp.inputs["Height"])
    disp.inputs["Midlevel"].default_value = 0.0
    disp.inputs["Scale"].default_value = 1.0
    nb.link(disp.outputs[0], out.inputs["Displacement"])
    return mat


def droplet_material(name, color, density, film=0.0):
    mat = bpy.data.materials.new(name)
    nb = NodeBuilder(mat.node_tree)
    nb.nodes.clear()
    out = nb.node("ShaderNodeOutputMaterial")
    surf = nb.node("ShaderNodeBsdfPrincipled")
    surf.inputs["Base Color"].default_value = lin(color)
    surf.inputs["Roughness"].default_value = 0.0
    surf.inputs["IOR"].default_value = 1.33
    surf.inputs["Transmission Weight"].default_value = 0.78
    surf.inputs["Coat Weight"].default_value = 1.0
    surf.inputs["Coat Roughness"].default_value = 0.0
    if film:
        surf.inputs["Thin Film Thickness"].default_value = film
        surf.inputs["Thin Film IOR"].default_value = 1.4
    nb.link(surf.outputs[0], out.inputs["Surface"])
    if density:
        vol = nb.node("ShaderNodeVolumePrincipled")
        vol.inputs["Absorption Color"].default_value = lin(color)
        vol.inputs["Color"].default_value = lin(color)
        vol.inputs["Density"].default_value = density
        nb.link(vol.outputs[0], out.inputs["Volume"])
    return mat


# -------------------------------------------------------------------------------- geometry
def amber_outline(n=96, overshoot=1.25):
    """Polygon of the amber half: the right side of the big circle (pushed out past the rim so the
    cut covers the ragged edge), the bottom small circle's right arc, the top one's left arc."""
    pts = []
    for i in range(n + 1):  # big arc, top -> bottom through the right
        a = math.pi / 2 - math.pi * i / n
        pts.append((overshoot * R * math.cos(a), overshoot * R * math.sin(a)))
    for i in range(n + 1):  # bottom small circle, (0,-R) -> (0,0) through x > 0
        a = -math.pi / 2 + math.pi * i / n
        pts.append((R / 2 * math.cos(a), -R / 2 + R / 2 * math.sin(a)))
    for i in range(1, n + 1):  # top small circle, (0,0) -> (0,R) through x < 0
        a = -math.pi / 2 - math.pi * i / n
        pts.append((R / 2 * math.cos(a), R / 2 + R / 2 * math.sin(a)))
    # no repeated points: zero-length edges make the exact boolean solver fail silently
    clean = [pts[0]]
    for q in pts[1:]:
        if math.dist(q, clean[-1]) > 1e-6:
            clean.append(q)
    if math.dist(clean[0], clean[-1]) < 1e-6:
        clean.pop()
    return clean


def prism(name, outline, depth=1.0):
    import bmesh

    bm = bmesh.new()
    bottom = [bm.verts.new((x, y, -depth)) for x, y in outline]
    top = [bm.verts.new((x, y, depth)) for x, y in outline]
    bm.faces.new(bottom[::-1])
    bm.faces.new(top)
    for i in range(len(outline)):
        j = (i + 1) % len(outline)
        bm.faces.new((bottom[i], bottom[j], top[j], top[i]))
    # the outline runs clockwise, so make every face point outward: an inside-out cutter makes
    # the exact boolean solver return the whole disc for DIFFERENCE
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    ob = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(ob)
    return ob


def make_halves():
    """Two real halves cut along the S-curve, so they can split apart and merge back. Each half
    keeps the shared liquid material; the colour mask is evaluated in object space, so it moves
    with its half."""
    mat = liquid_material()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=256, ring_count=128, radius=R)
    base = bpy.context.active_object
    base.scale = (1.0, 1.0, DOME / R)
    bpy.ops.object.transform_apply(scale=True)
    # ragged, splashy rim baked into the mesh (static, so animation frames reuse the geometry)
    from mathutils import noise as mnoise

    for v in base.data.vertices:
        r = math.hypot(v.co.x, v.co.y)
        w = min(1.0, max(0.0, (r - 0.9 * R) / (0.1 * R)))
        if w > 0:
            w = w * w * (3 - 2 * w)
            push = (mnoise.noise(v.co * 2.6) * 0.5 + 0.05) * 0.05 * w
            radial = Vector((v.co.x, v.co.y, 0.0)).normalized()
            v.co += radial * push
    bpy.ops.object.transform_apply(scale=True)
    cutter = prism("AmberCutter", amber_outline())
    halves = {}
    for name, op in (("Amber", "INTERSECT"), ("Indigo", "DIFFERENCE")):
        ob = base.copy()
        ob.data = base.data.copy()
        ob.name = f"Half{name}"
        bpy.context.collection.objects.link(ob)
        mod = ob.modifiers.new("Cut", "BOOLEAN")
        mod.operation, mod.solver, mod.object = op, "EXACT", cutter
        bpy.context.view_layer.objects.active = ob
        bpy.ops.object.modifier_apply(modifier="Cut")
        bpy.ops.object.shade_smooth()
        sub = ob.modifiers.new("Subdiv", "SUBSURF")
        sub.levels = sub.render_levels = 1 if FAST else 2
        ob.data.materials.clear()
        ob.data.materials.append(mat)
        halves[name] = ob
    bpy.data.objects.remove(cutter)
    bpy.data.objects.remove(base)
    return halves


def front_z(x, y):
    """Height of the disc's front surface at (x, y)."""
    r2 = min(1.0, (x * x + y * y) / (R * R))
    return DOME * math.sqrt(max(0.0, 1.0 - r2))


_SPHERES: dict[str, bpy.types.Mesh] = {}


def sphere(name, loc, radius, mat, squash=1.0):
    """A droplet. Meshes are shared per material, so hundreds of droplets stay cheap."""
    if mat.name not in _SPHERES:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1.0)
        tmp = bpy.context.active_object
        bpy.ops.object.shade_smooth()
        mesh = tmp.data
        mesh.name = f"Sphere_{mat.name}"
        mesh.materials.append(mat)
        bpy.data.objects.remove(tmp)
        _SPHERES[mat.name] = mesh
    ob = bpy.data.objects.new(name, _SPHERES[mat.name])
    ob.location = loc
    ob.scale = (radius, radius, radius * squash)
    bpy.context.collection.objects.link(ob)
    return ob


def seam_points(n):
    """Points along the S-seam: the top small circle's left arc, then the bottom one's right arc."""
    pts = []
    for i in range(n):
        t = i / (n - 1)
        if t < 0.5:  # top arc from (0, R) to (0, 0), bulging left
            a = math.pi / 2 + math.pi * (t / 0.5)
            pts.append((R / 2 * math.cos(a), R / 2 + R / 2 * math.sin(a)))
        else:  # bottom arc from (0, 0) to (0, -R), bulging right
            a = math.pi / 2 - math.pi * ((t - 0.5) / 0.5)
            pts.append((R / 2 * math.cos(a), -R / 2 + R / 2 * math.sin(a)))
    return pts


def build_bubbles():
    mats = [droplet_material("BubbleTeal", (0.25, 0.75, 0.8), 1.5, film=520),
            droplet_material("BubbleBlue", (0.15, 0.35, 0.9), 2.5, film=430),
            droplet_material("BubbleAmber", (1.0, 0.55, 0.12), 2.0, film=610),
            droplet_material("BubbleClear", (0.7, 0.85, 1.0), 0.6, film=480)]
    for i, (x, y) in enumerate(seam_points(320)):
        for _ in range(rng.choice((1, 1, 2))):
            # mostly fine froth, a few larger beads
            r = rng.choice((0.003, 0.004, 0.005, 0.006, 0.008, 0.01, 0.013, 0.018, 0.026)) * rng.uniform(0.75, 1.2)
            spread = 0.012 + 0.9 * r  # big bubbles hug the seam, tiny ones scatter wider
            ox, oy = x + rng.gauss(0, spread * 1.6), y + rng.gauss(0, spread * 1.6)
            if ox * ox + oy * oy > (R * 0.995) ** 2:
                continue
            z = front_z(ox, oy) + r * rng.uniform(-0.1, 0.55)
            sphere(f"Bubble{i}", (ox, oy, z), r, rng.choices(mats, weights=(5, 4, 2, 1))[0], squash=0.85)


def build_eye_splash():
    blue = droplet_material("DropIndigo", INDIGO, 30.0)
    gold = droplet_material("DropGold", GOLD, 12.0)
    for cy, mat in ((R / 2, blue), (-R / 2, gold)):
        for k in range(26 if FAST else 60):
            a = rng.uniform(0, 2 * math.pi)
            d = EYE_R * rng.uniform(0.95, 1.35)
            x, y = d * math.cos(a), cy + d * math.sin(a)
            r = rng.uniform(0.004, 0.016)
            sphere(f"Splash{cy}_{k}", (x, y, front_z(x, y) + r * 0.6), r, mat)


def build_stray_droplets():
    gold = droplet_material("StrayGold", GOLD, 8.0)
    blue = droplet_material("StrayBlue", INDIGO, 18.0)
    for k in range(28):
        a = rng.uniform(0, 2 * math.pi)
        d = rng.uniform(1.08, 1.75)
        x, y = d * math.cos(a), d * math.sin(a)
        right = x > -0.15 and y > -0.6
        r = rng.choice((0.006, 0.01, 0.015, 0.022, 0.03))
        sphere(f"Stray{k}", (x, y, rng.uniform(-0.05, 0.12)), r, gold if right else blue)
    # splashes where the seam meets the rim (top and bottom), like the reference
    for cy in (R, -R):
        for k in range(10):
            x = rng.gauss(0.04 if cy > 0 else -0.04, 0.06)
            y = cy * rng.uniform(0.99, 1.08)
            sphere(f"RimSplash{cy}_{k}", (x, y, rng.uniform(0.0, 0.08)), rng.uniform(0.005, 0.015),
                   gold if cy < 0 else blue)


# ------------------------------------------------------------------------- world + lights
def build_world():
    world = bpy.data.worlds.new("Eggshell")
    scene.world = world
    nb = NodeBuilder(world.node_tree)
    nb.nodes.clear()
    out = nb.node("ShaderNodeOutputWorld")
    lp = nb.node("ShaderNodeLightPath")
    seen = nb.node("ShaderNodeBackground")  # what the camera sees: exact eggshell
    seen.inputs["Color"].default_value = lin(EGGSHELL)
    seen.inputs["Strength"].default_value = 1.0
    light = nb.node("ShaderNodeBackground")  # what lights the liquid: soft near-white studio
    light.inputs["Color"].default_value = lin((0.98, 0.97, 0.95))
    light.inputs["Strength"].default_value = 0.45
    mix = nb.node("ShaderNodeMixShader")
    nb.link(lp.outputs["Is Camera Ray"], mix.inputs[0])
    nb.link(light.outputs[0], mix.inputs[1])
    nb.link(seen.outputs[0], mix.inputs[2])
    nb.link(mix.outputs[0], out.inputs[0])


def area(name, loc, size, energy, color=(1, 1, 1), size_y=None):
    data = bpy.data.lights.new(name, "AREA")
    data.shape = "RECTANGLE"
    data.size = size
    data.size_y = size_y or size
    data.energy = energy
    data.color = color
    ob = bpy.data.objects.new(name, data)
    ob.location = loc
    bpy.context.collection.objects.link(ob)
    ob.rotation_euler = (Vector((0, 0, 0)) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    return ob


def build_lights():
    area("KeySoftbox", (-3.0, 3.5, 6.0), 1.4, 650, size_y=0.9)  # top-left highlight: smaller, crisper glints
    area("FillSoftbox", (4.0, -1.0, 5.0), 4.0, 260, color=(1.0, 0.97, 0.92))
    area("RimStrip", (0.0, -5.0, 2.2), 6.0, 180, size_y=1.6)  # soft and dim: no stripe reflections
    area("TopKick", (1.5, 2.5, 7.0), 0.8, 300)


def build_camera():
    cam = bpy.data.cameras.new("Cam")
    cam.lens = 100
    cam.sensor_width = 36
    ob = bpy.data.objects.new("Cam", cam)
    ob.location = (0.0, -0.45, 11.4)  # the disc fills ~86% of the frame height, like the reference
    bpy.context.collection.objects.link(ob)
    ob.rotation_euler = (Vector((0, 0, 0)) - ob.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = ob


# ------------------------------------------------------------------------------------ main
# ------------------------------------------------------------------------------ animation
# The AMS story with the real example: Java `Order` fields cross into Python `OrderRecord`.
FIELD_PAIRS = [  # provider field, consumer field, confidence (one recorded Groq run)
    ("orderId", "order_ref", 0.96), ("customerId", "client_id", 0.96), ("totalAmount", "amount_due", 0.95),
    ("currencyCode", "currency", 0.95), ("createdAt", "placed_at", 0.95), ("orderStatus", "status", 0.90),
    ("shippingCity", "city", 0.95),
]
STORY_FRAMES = 240
SPLIT_AT, SPLIT_END = 36, 84  # halves drift apart
CROSS_AT, CROSS_STEP, CROSS_LEN = 84, 11, 40  # droplet i leaves at CROSS_AT + i*CROSS_STEP
MERGE_AT, MERGE_END = 176, 222  # halves flow back together
APART = Vector((0.42, 0.14, 0.0))  # amber offset when fully split; indigo gets the opposite


def key_loc(ob, frame, loc):
    ob.location = loc
    ob.keyframe_insert("location", frame=frame)


def all_fcurves(ob):
    """F-curves of an object's action (Blender 5 keeps them in layered-action channelbags)."""
    action = ob.animation_data.action if ob.animation_data else None
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                out.extend(bag.fcurves)
    return out


def add_drift(ob, strength=0.012, scale=46.0):
    """Zero-gravity bob: slow noise on each location channel, a different phase per object."""
    if not all_fcurves(ob):
        ob.keyframe_insert("location", frame=1)
    for fc in all_fcurves(ob):
        if fc.data_path == "location":
            mod = fc.modifiers.new("NOISE")
            mod.strength, mod.scale, mod.phase = strength, scale, rng.uniform(0, 100)


def build_field_droplets(rig):
    """Seven droplets that start in the indigo (provider) half, ride it while it splits, float across
    the gap one at a time turning amber on the way, and settle into the amber (consumer) half."""
    pts = seam_points(201)
    drops = []
    for i, (prov, cons, conf) in enumerate(FIELD_PAIRS):
        mat = droplet_material(f"Field_{prov}", INDIGO, 3.0)
        base = mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"]
        vol = mat.node_tree.nodes["Principled Volume"]
        sx, sy = pts[round((0.12 + 0.76 * i / (len(FIELD_PAIRS) - 1)) * 200)]
        cy = R / 2 if sy > 0 else -R / 2  # which small circle this stretch of seam belongs to
        n = Vector((sx, sy - cy, 0.0)).normalized()
        # the indigo side is outside the top circle but inside the bottom one
        indigo_dir = n if sy > 0 else -n
        inside_indigo = Vector((sx, sy, 0.0)) + indigo_dir * 0.07
        inside_amber = Vector((sx, sy, 0.0)) - indigo_dir * 0.07
        r = 0.052
        for v in (inside_indigo, inside_amber):
            v.z = front_z(v.x, v.y) + r * 0.8
        ob = sphere(f"Field{i}", tuple(inside_indigo), r, mat)
        ob.parent = rig
        leave = CROSS_AT + i * CROSS_STEP
        arrive = leave + CROSS_LEN
        key_loc(ob, 1, inside_indigo)
        key_loc(ob, SPLIT_AT, inside_indigo)
        key_loc(ob, SPLIT_END, inside_indigo - APART)
        if leave > SPLIT_END:
            key_loc(ob, leave, inside_indigo - APART)
        mid = (inside_indigo - APART + inside_amber + APART) / 2
        mid.z += 0.3
        key_loc(ob, (leave + arrive) // 2, mid)
        key_loc(ob, arrive, inside_amber + APART)
        key_loc(ob, MERGE_AT, inside_amber + APART)
        key_loc(ob, MERGE_END, inside_amber)
        for frame, col in ((leave + 14, INDIGO), (leave + 26, AMBER)):  # turns amber mid-flight
            for sock in (base, vol.inputs["Color"], vol.inputs["Absorption Color"]):
                sock.default_value = lin(col)
                sock.keyframe_insert("default_value", frame=frame)
        drops.append({"ob": ob, "provider": prov, "consumer": cons, "confidence": conf,
                      "switch": (leave + arrive) // 2})
    return drops


def animate_story(halves):
    for name, sign in (("Amber", 1), ("Indigo", -1)):
        for frame, amount in ((1, 0), (SPLIT_AT, 0), (SPLIT_END, 1), (MERGE_AT, 1), (MERGE_END, 0)):
            key_loc(halves[name], frame, APART * (sign * amount))


def export_story_json(drops, path):
    """Per frame, each field droplet's position in the frame (0..1, top-left origin) and which name
    it carries (1 = provider, 2 = consumer), so the page can pin live labels to the footage."""
    import json

    from bpy_extras.object_utils import world_to_camera_view

    frames = []
    for f in range(1, STORY_FRAMES + 1):
        scene.frame_set(f)
        row = []
        for d in drops:
            co = world_to_camera_view(scene, scene.camera, d["ob"].matrix_world.translation)
            row.append([round(co.x, 4), round(1 - co.y, 4), 1 if f < d["switch"] else 2])
        frames.append(row)
    data = {"fps": FPS, "frames": STORY_FRAMES, "split": [SPLIT_AT, SPLIT_END], "merge": [MERGE_AT, MERGE_END],
            "droplets": [{k: d[k] for k in ("provider", "consumer", "confidence", "switch")} for d in drops],
            "positions": frames}
    path.write_text(json.dumps(data), encoding="utf8")
    print(f"[liquid] wrote {path}")


build_world()
halves = make_halves()
build_bubbles()
build_eye_splash()
build_stray_droplets()
# one parent for the disc and every droplet, so they rotate together (the reference sits a few
# degrees off-axis); rotating only the disc would slide the seam bubbles off the seam
rig = bpy.data.objects.new("Taijitu", None)
bpy.context.collection.objects.link(rig)
for ob in list(bpy.context.collection.objects):
    if ob.type == "MESH":
        ob.parent = rig
# eye splashes ride their own half (the indigo eye sits in the amber half, the gold eye in the indigo)
for ob in bpy.context.collection.objects:
    if ob.name.startswith("Splash"):
        ob.parent = halves["Amber"] if ob.location.y > 0 else halves["Indigo"]
rig.rotation_euler = (0.0, 0.0, math.radians(-17))  # matches the reference's eye positions
build_lights()
build_camera()

ANIM = ARGS[ARGS.index("--anim") + 1] if "--anim" in ARGS else None
if ANIM:
    drops = build_field_droplets(rig) if ANIM == "story" else []
    if ANIM == "story":
        animate_story(halves)
    for ob in list(bpy.context.collection.objects):
        if ob.type == "MESH" and ob.name.startswith(("Bubble", "Stray", "RimSplash", "Splash")):
            add_drift(ob, strength=0.006 if ob.name.startswith("Bubble") else 0.012)
    total = STORY_FRAMES if ANIM == "story" else 144
    first, last = 1, total
    if "--frames" in ARGS:
        first, last = (int(x) for x in ARGS[ARGS.index("--frames") + 1].split("-"))
    scene.frame_start, scene.frame_end = first, last
    scene.render.fps = FPS
    seq = OUT / "frames" / f"{ANIM}_{RES_X}"
    seq.mkdir(parents=True, exist_ok=True)
    if drops:
        export_story_json(drops, OUT / "story.json")
    scene.render.filepath = str(seq / "f_")
    scene.render.use_overwrite = False  # resumable: frames already on disk are skipped
    scene.render.use_placeholder = True
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT / f"liquid_{ANIM}.blend"))
    if "--no-render" not in ARGS:
        bpy.ops.render.render(animation=True)
    print(f"[liquid] {ANIM} frames {first}-{last} -> {seq}")
else:
    if SPLIT:  # test pose: halves drift apart
        halves["Amber"].location = APART * SPLIT
        halves["Indigo"].location = APART * -SPLIT
    tag = ("preview" if FAST else f"hero_{RES_X}") + (f"_split{SPLIT:g}" if SPLIT else "")
    scene.render.filepath = str(OUT / f"{tag}.png")
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT / "liquid.blend"))
    bpy.ops.render.render(write_still=True)
    print(f"[liquid] wrote {scene.render.filepath}")
