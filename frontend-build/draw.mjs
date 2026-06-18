// MoonbaseDraw — lightweight in-note SVG sketch canvas.
// Exposes window.MoonbaseDraw = { open({ svg?, onSave }) }
// No external deps; styled via the app's CSS custom properties.

const PALETTE = ['#d4a039', '#e5ddd3', '#5fa87a', '#c45c3c', '#5b8fb0', '#1a1712'];
const STROKE_WIDTHS = [2, 4, 8];
const SVG_NS = 'http://www.w3.org/2000/svg';
const VB_W = 1000;
const VB_H = 700;

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

// Map a pointer clientX/Y into the SVG viewBox coordinate space.
function toVB(svgRect, cx, cy) {
  const scaleX = VB_W / svgRect.width;
  const scaleY = VB_H / svgRect.height;
  return {
    x: Math.round((cx - svgRect.left) * scaleX),
    y: Math.round((cy - svgRect.top) * scaleY),
  };
}

function open({ svg = null, onSave } = {}) {
  // ---- State ----------------------------------------------------------------
  let activeTool = 'pen';
  let activeColor = PALETTE[0];
  let activeWidth = 2;
  let drawing = false;
  let currentEl = null;
  let startPt = null;
  let penPoints = [];
  // undoStack holds references to top-level shape children we appended.
  const undoStack = [];

  // ---- Overlay --------------------------------------------------------------
  const overlay = document.createElement('div');
  Object.assign(overlay.style, {
    position: 'fixed', inset: '0', zIndex: '600',
    display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
    background: 'rgba(0,0,0,0.65)',
    fontFamily: 'var(--font-ui, system-ui, sans-serif)',
  });

  // ---- Modal container ------------------------------------------------------
  const modal = document.createElement('div');
  Object.assign(modal.style, {
    display: 'flex', flexDirection: 'column',
    background: 'var(--bg-card, #23201c)',
    border: '1px solid var(--border, #4a4030)',
    borderRadius: 'var(--radius, 10px)',
    boxShadow: 'var(--shadow, 0 8px 24px rgba(0,0,0,0.5))',
    padding: '12px',
    gap: '10px',
    maxWidth: 'calc(100vw - 32px)',
    maxHeight: 'calc(100vh - 32px)',
    userSelect: 'none',
  });

  // ---- Toolbar --------------------------------------------------------------
  const toolbar = document.createElement('div');
  Object.assign(toolbar.style, {
    display: 'flex', flexWrap: 'wrap', gap: '6px', alignItems: 'center',
    paddingBottom: '8px',
    borderBottom: '1px solid var(--border, #4a4030)',
  });

  // Tool buttons
  const TOOLS = [
    { id: 'pen',     label: '✏️ Pen' },
    { id: 'line',    label: '╱ Line' },
    { id: 'rect',    label: '▭ Rect' },
    { id: 'ellipse', label: '⬭ Ellipse' },
    { id: 'text',    label: 'T Text' },
    { id: 'eraser',  label: '⌫ Eraser' },
  ];

  const toolBtns = {};
  for (const tool of TOOLS) {
    const btn = document.createElement('button');
    btn.textContent = tool.label;
    btn.dataset.tool = tool.id;
    styleToolBtn(btn, tool.id === activeTool);
    btn.addEventListener('click', () => {
      activeTool = tool.id;
      for (const [tid, b] of Object.entries(toolBtns)) styleToolBtn(b, tid === activeTool);
    });
    toolBtns[tool.id] = btn;
    toolbar.appendChild(btn);
  }

  // Divider
  toolbar.appendChild(divider());

  // Palette swatches
  const swatchBtns = {};
  for (const color of PALETTE) {
    const sw = document.createElement('button');
    sw.title = color;
    Object.assign(sw.style, {
      width: '22px', height: '22px', borderRadius: '50%',
      background: color, border: '2px solid transparent',
      cursor: 'pointer', padding: '0', flexShrink: '0',
      outline: 'none',
    });
    if (color === activeColor) sw.style.border = '2px solid var(--accent, #d4a039)';
    sw.addEventListener('click', () => {
      activeColor = color;
      for (const [c, b] of Object.entries(swatchBtns)) {
        b.style.border = c === activeColor ? '2px solid var(--accent, #d4a039)' : '2px solid transparent';
      }
    });
    swatchBtns[color] = sw;
    toolbar.appendChild(sw);
  }

  // Divider
  toolbar.appendChild(divider());

  // Stroke width selector
  const swLabel = document.createElement('span');
  swLabel.textContent = 'Width:';
  Object.assign(swLabel.style, { color: 'var(--text-secondary, #a0937e)', fontSize: '13px' });
  toolbar.appendChild(swLabel);

  const widthBtns = {};
  for (const w of STROKE_WIDTHS) {
    const btn = document.createElement('button');
    btn.textContent = w + 'px';
    styleToolBtn(btn, w === activeWidth);
    btn.addEventListener('click', () => {
      activeWidth = w;
      for (const [ww, b] of Object.entries(widthBtns)) styleToolBtn(b, Number(ww) === activeWidth);
    });
    widthBtns[w] = btn;
    toolbar.appendChild(btn);
  }

  // Divider
  toolbar.appendChild(divider());

  // Undo / Clear
  const undoBtn = makeBtn('↩ Undo', () => {
    if (undoStack.length) {
      const el = undoStack.pop();
      if (el && el.parentNode === svgEl_) svgEl_.removeChild(el);
    }
  });
  const clearBtn = makeBtn('🗑 Clear', () => {
    while (svgEl_.firstChild) svgEl_.removeChild(svgEl_.firstChild);
    undoStack.length = 0;
  });
  toolbar.appendChild(undoBtn);
  toolbar.appendChild(clearBtn);

  // Spacer
  const spacer = document.createElement('span');
  spacer.style.flex = '1';
  toolbar.appendChild(spacer);

  // Save / Cancel
  const saveBtn = makeBtn('💾 Save', doSave, true);
  const cancelBtn = makeBtn('✕ Cancel', closeOverlay);
  toolbar.appendChild(saveBtn);
  toolbar.appendChild(cancelBtn);

  // ---- SVG drawing surface --------------------------------------------------
  const svgEl_ = document.createElementNS(SVG_NS, 'svg');
  svgEl_.setAttribute('xmlns', SVG_NS);
  svgEl_.setAttribute('viewBox', `0 0 ${VB_W} ${VB_H}`);
  Object.assign(svgEl_.style, {
    display: 'block',
    background: 'var(--bg-secondary, #f5f0e8)',
    border: '1px solid var(--border, #4a4030)',
    borderRadius: 'var(--radius-sm, 6px)',
    touchAction: 'none',
    cursor: 'crosshair',
    // Responsive: fill available space in the modal.
    width: 'min(calc(100vw - 56px), calc((100vh - 200px) * 1000/700))',
    height: 'auto',
    aspectRatio: `${VB_W} / ${VB_H}`,
  });

  // Load existing SVG content for re-editing.
  if (svg && typeof svg === 'string') {
    try {
      const parser = new DOMParser();
      const doc = parser.parseFromString(svg, 'image/svg+xml');
      const parsedRoot = doc.documentElement;
      // DOMParser never throws — detect parse failure via parseerror element.
      if (
        parsedRoot.nodeName === 'parseerror' ||
        parsedRoot.querySelector('parseerror')
      ) {
        throw new Error('SVG parse error');
      }
      // Copy inner children of the parsed <svg> into our svgEl_.
      // We clone so they are part of our document.
      const imported = Array.from(parsedRoot.childNodes).map(
        (n) => document.importNode(n, true)
      );
      for (const n of imported) svgEl_.appendChild(n);
      // Loaded shapes are not individually undoable (per spec), but live as children
      // so Clear removes them all and Eraser can remove them individually.
    } catch (_) {
      // Ignore parse errors — start blank.
    }
  }

  // ---- Pointer event handlers -----------------------------------------------
  svgEl_.addEventListener('pointerdown', onPointerDown);
  svgEl_.addEventListener('pointermove', onPointerMove);
  svgEl_.addEventListener('pointerup', onPointerUp);
  svgEl_.addEventListener('pointercancel', onPointerUp);

  function getVB(e) {
    const rect = svgEl_.getBoundingClientRect();
    return toVB(rect, e.clientX, e.clientY);
  }

  function shapeAttrs(extra = {}) {
    return {
      fill: 'none',
      stroke: activeColor,
      'stroke-width': String(activeWidth),
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
      ...extra,
    };
  }

  function onPointerDown(e) {
    // Eraser: remove the clicked shape element.
    if (activeTool === 'eraser') {
      const target = e.target;
      if (target !== svgEl_ && target.parentNode === svgEl_) {
        // Remove from undoStack if present.
        const idx = undoStack.indexOf(target);
        if (idx !== -1) undoStack.splice(idx, 1);
        svgEl_.removeChild(target);
      }
      return;
    }

    if (activeTool === 'text') {
      const pt = getVB(e);
      const str = window.prompt('Enter text:');
      if (!str) return;
      const txt = svgEl('text', {
        x: String(pt.x),
        y: String(pt.y),
        fill: activeColor,
        'font-size': String(Math.max(14, activeWidth * 6)),
        'font-family': 'Lexend, "Segoe UI", system-ui, sans-serif',
      });
      txt.textContent = str;
      svgEl_.appendChild(txt);
      undoStack.push(txt);
      return;
    }

    drawing = true;
    svgEl_.setPointerCapture(e.pointerId);
    startPt = getVB(e);
    const pt = startPt;

    if (activeTool === 'pen') {
      penPoints = [pt];
      currentEl = svgEl('path', shapeAttrs({ d: `M ${pt.x} ${pt.y}` }));
      svgEl_.appendChild(currentEl);
      undoStack.push(currentEl);
    } else if (activeTool === 'line') {
      currentEl = svgEl('line', shapeAttrs({
        x1: String(pt.x), y1: String(pt.y),
        x2: String(pt.x), y2: String(pt.y),
      }));
      svgEl_.appendChild(currentEl);
      undoStack.push(currentEl);
    } else if (activeTool === 'rect') {
      currentEl = svgEl('rect', shapeAttrs({
        x: String(pt.x), y: String(pt.y), width: '0', height: '0',
      }));
      svgEl_.appendChild(currentEl);
      undoStack.push(currentEl);
    } else if (activeTool === 'ellipse') {
      currentEl = svgEl('ellipse', shapeAttrs({
        cx: String(pt.x), cy: String(pt.y), rx: '0', ry: '0',
      }));
      svgEl_.appendChild(currentEl);
      undoStack.push(currentEl);
    }
  }

  function onPointerMove(e) {
    if (!drawing || !currentEl) return;
    const pt = getVB(e);

    if (activeTool === 'pen') {
      penPoints.push(pt);
      // Build path d string: "M x0 y0 L x1 y1 L x2 y2 ..."
      const d = penPoints.map((p, i) => (i === 0 ? `M ${p.x} ${p.y}` : `L ${p.x} ${p.y}`)).join(' ');
      currentEl.setAttribute('d', d);
    } else if (activeTool === 'line') {
      currentEl.setAttribute('x2', String(pt.x));
      currentEl.setAttribute('y2', String(pt.y));
    } else if (activeTool === 'rect') {
      const x = Math.min(startPt.x, pt.x);
      const y = Math.min(startPt.y, pt.y);
      const w = Math.abs(pt.x - startPt.x);
      const h = Math.abs(pt.y - startPt.y);
      currentEl.setAttribute('x', String(x));
      currentEl.setAttribute('y', String(y));
      currentEl.setAttribute('width', String(w));
      currentEl.setAttribute('height', String(h));
    } else if (activeTool === 'ellipse') {
      const rx = Math.abs(pt.x - startPt.x) / 2;
      const ry = Math.abs(pt.y - startPt.y) / 2;
      const cx = (startPt.x + pt.x) / 2;
      const cy = (startPt.y + pt.y) / 2;
      currentEl.setAttribute('cx', String(Math.round(cx)));
      currentEl.setAttribute('cy', String(Math.round(cy)));
      currentEl.setAttribute('rx', String(Math.round(rx)));
      currentEl.setAttribute('ry', String(Math.round(ry)));
    }
  }

  function onPointerUp() {
    drawing = false;
    currentEl = null;
    startPt = null;
    penPoints = [];
  }

  // ---- Save / Close ---------------------------------------------------------
  function doSave() {
    if (typeof onSave === 'function') {
      onSave(svgEl_.outerHTML);
    }
    closeOverlay();
  }

  function closeOverlay() {
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    document.removeEventListener('keydown', onKeyDown);
  }

  function onKeyDown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeOverlay();
    }
  }
  document.addEventListener('keydown', onKeyDown);

  // Click on backdrop (not modal) closes without saving.
  overlay.addEventListener('pointerdown', (e) => {
    if (e.target === overlay) closeOverlay();
  });

  // ---- Assemble & mount -----------------------------------------------------
  modal.appendChild(toolbar);
  modal.appendChild(svgEl_);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  // Focus the modal so Escape works immediately.
  modal.tabIndex = -1;
  modal.focus();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function styleToolBtn(btn, active) {
  Object.assign(btn.style, {
    padding: '4px 8px',
    fontSize: '13px',
    cursor: 'pointer',
    border: '1px solid var(--border, #4a4030)',
    borderRadius: 'var(--radius-sm, 6px)',
    background: active ? 'var(--accent, #d4a039)' : 'var(--bg-hover, #2e2a22)',
    color: active ? 'var(--bg-primary, #1a1712)' : 'var(--text-primary, #e5ddd3)',
    fontFamily: 'var(--font-ui, system-ui, sans-serif)',
    fontWeight: active ? '700' : '400',
    transition: 'background 0.1s',
    outline: 'none',
    flexShrink: '0',
  });
}

function makeBtn(label, onClick, isPrimary = false) {
  const btn = document.createElement('button');
  btn.textContent = label;
  styleToolBtn(btn, isPrimary);
  btn.addEventListener('click', onClick);
  return btn;
}

function divider() {
  const d = document.createElement('span');
  Object.assign(d.style, {
    width: '1px', height: '20px',
    background: 'var(--border, #4a4030)',
    flexShrink: '0', alignSelf: 'center',
  });
  return d;
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------
const MoonbaseDraw = { open };
if (typeof window !== 'undefined') window.MoonbaseDraw = MoonbaseDraw;
export default MoonbaseDraw;
