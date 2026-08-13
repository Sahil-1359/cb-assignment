/* Wireframe sphere backdrop.
 *
 * Plain canvas 2D with a hand-rolled 3D projection rather than Three.js: the
 * whole effect is a few hundred projected points, and pulling a vendor bundle
 * into this repo for that would be the wrong trade.
 *
 * Behaviour: slow idle rotation that accelerates and brightens as the cursor
 * approaches the sphere's centre. Both the speed and the glow are eased toward
 * a target each frame rather than tracking the pointer directly, so the sphere
 * carries momentum instead of snapping.
 *
 * Everything below is decorative. It is wrapped so that any failure leaves the
 * page exactly as it would be with JavaScript disabled.
 */
(function () {
  "use strict";

  try {
    var canvas = document.getElementById("sphere-canvas");
    if (!canvas || !canvas.getContext) return;

    var context = canvas.getContext("2d");
    if (!context) return;

    var reduceMotion = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var isSmall = window.innerWidth < 760;

    // Fewer rings on small screens: same shape, less per-frame work.
    var LAT_LINES = isSmall ? 9 : 15;
    var LON_LINES = isSmall ? 12 : 22;
    var SEGMENTS = isSmall ? 36 : 60;

    var IDLE_SPEED = 0.0016;
    var ACTIVE_SPEED = 0.0075;
    var EASING = 0.045;          // how fast speed/glow approach their target

    var rotation = 0;
    var speed = IDLE_SPEED;
    var targetSpeed = IDLE_SPEED;
    var glow = 0;
    var targetGlow = 0;
    var tilt = -0.32;

    var width = 0;
    var height = 0;
    var radius = 0;
    var centreX = 0;
    var centreY = 0;

    function resize() {
      var ratio = window.devicePixelRatio || 1;
      var rect = canvas.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      if (!width || !height) return false;

      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);

      centreX = width / 2;
      centreY = height / 2;
      radius = Math.min(width, height) * 0.42;
      return true;
    }

    // Rotate around Y, then tilt around X, then project.
    function project(x, y, z, angle) {
      var cosA = Math.cos(angle);
      var sinA = Math.sin(angle);
      var rx = x * cosA - z * sinA;
      var rz = x * sinA + z * cosA;

      var cosT = Math.cos(tilt);
      var sinT = Math.sin(tilt);
      var ry = y * cosT - rz * sinT;
      var rz2 = y * sinT + rz * cosT;

      // Mild perspective so the far side reads as further away.
      var depth = 1 / (1 + rz2 * 0.35);
      return {
        x: centreX + rx * radius * depth,
        y: centreY + ry * radius * depth,
        depth: rz2
      };
    }

    function strokeRing(points, angle, baseAlpha) {
      var previous = null;
      for (var i = 0; i < points.length; i++) {
        var p = project(points[i][0], points[i][1], points[i][2], angle);
        if (previous) {
          // Front-facing segments are brighter than the ones behind.
          var facing = (1 - (p.depth + 1) / 2);
          var alpha = baseAlpha * (0.22 + facing * 0.78);
          context.strokeStyle = "rgba(255, 51, 102, " + alpha.toFixed(3) + ")";
          context.beginPath();
          context.moveTo(previous.x, previous.y);
          context.lineTo(p.x, p.y);
          context.stroke();
        }
        previous = p;
      }
    }

    // Precompute the ring geometry once; only the rotation angle changes.
    var latitudes = [];
    var longitudes = [];

    for (var a = 1; a < LAT_LINES; a++) {
      var phi = (a / LAT_LINES) * Math.PI;
      var ring = [];
      for (var s = 0; s <= SEGMENTS; s++) {
        var theta = (s / SEGMENTS) * Math.PI * 2;
        ring.push([
          Math.sin(phi) * Math.cos(theta),
          Math.cos(phi),
          Math.sin(phi) * Math.sin(theta)
        ]);
      }
      latitudes.push(ring);
    }

    for (var b = 0; b < LON_LINES; b++) {
      var theta2 = (b / LON_LINES) * Math.PI * 2;
      var meridian = [];
      for (var t = 0; t <= SEGMENTS; t++) {
        var phi2 = (t / SEGMENTS) * Math.PI;
        meridian.push([
          Math.sin(phi2) * Math.cos(theta2),
          Math.cos(phi2),
          Math.sin(phi2) * Math.sin(theta2)
        ]);
      }
      longitudes.push(meridian);
    }

    function draw() {
      context.clearRect(0, 0, width, height);
      context.lineWidth = 1;

      var base = 0.20 + glow * 0.42;

      for (var i = 0; i < latitudes.length; i++) {
        strokeRing(latitudes[i], rotation, base);
      }
      for (var j = 0; j < longitudes.length; j++) {
        strokeRing(longitudes[j], rotation, base * 0.82);
      }
    }

    function frame() {
      speed += (targetSpeed - speed) * EASING;
      glow += (targetGlow - glow) * EASING;
      rotation += speed;
      draw();
      window.requestAnimationFrame(frame);
    }

    if (!resize()) return;

    if (reduceMotion) {
      // One static frame, no listeners, no loop.
      glow = 0.25;
      draw();
      return;
    }

    window.addEventListener("resize", function () {
      if (resize()) draw();
    });

    window.addEventListener("pointermove", function (event) {
      var rect = canvas.getBoundingClientRect();
      var dx = event.clientX - (rect.left + rect.width / 2);
      var dy = event.clientY - (rect.top + rect.height / 2);
      var distance = Math.sqrt(dx * dx + dy * dy);

      // 0 at the centre, 1 once the cursor is more than ~1.6 radii away.
      var falloff = Math.min(1, distance / (radius * 1.6));
      var proximity = 1 - falloff;

      targetSpeed = IDLE_SPEED + (ACTIVE_SPEED - IDLE_SPEED) * proximity;
      targetGlow = proximity;
    }, { passive: true });

    window.requestAnimationFrame(frame);
  } catch (error) {
    /* Decorative only -- never break the page over it. */
  }
})();
