"""HTML template parts for the ARVIS specialization report."""

CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root {
  --bg: #06080d; --surface: #0d1117; --surface2: #161b22; --surface3: #1c2333;
  --border: rgba(255,255,255,0.06); --border2: rgba(255,255,255,0.1);
  --text: #e6edf3; --text2: #8b949e; --text3: #484f58;
  --accent: #58a6ff; --green: #3fb950; --red: #f85149;
  --yellow: #d29922; --purple: #bc8cff; --cyan: #39d2c0; --orange: #f0883e;
  --grad1: linear-gradient(135deg, #58a6ff 0%, #bc8cff 100%);
  --grad2: linear-gradient(135deg, #3fb950 0%, #39d2c0 100%);
  --grad3: linear-gradient(135deg, #f0883e 0%, #f85149 100%);
  --glow: 0 0 20px rgba(88,166,255,0.15);
  --radius: 16px; --radius-sm: 10px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);
  line-height:1.65;-webkit-font-smoothing:antialiased;font-size:15px}
.container{max-width:1280px;margin:0 auto;padding:32px 24px}

/* ── Nav ── */
.nav{position:fixed;top:0;left:0;right:0;z-index:100;
  background:rgba(6,8,13,0.85);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);
  padding:0 24px;display:flex;align-items:center;height:52px;gap:24px}
.nav-brand{font-weight:700;font-size:0.95rem;background:var(--grad1);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;white-space:nowrap}
.nav-links{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none}
.nav-links::-webkit-scrollbar{display:none}
.nav-link{color:var(--text2);font-size:0.78rem;padding:6px 12px;border-radius:6px;
  text-decoration:none;white-space:nowrap;transition:all .15s}
.nav-link:hover{color:var(--text);background:var(--surface2)}
body{padding-top:60px}

/* ── Header ── */
.header{text-align:center;padding:56px 0 40px;position:relative}
.header::before{content:'';position:absolute;top:0;left:50%;transform:translateX(-50%);
  width:160px;height:3px;background:var(--grad1);border-radius:2px}
.header h1{font-size:2.8rem;font-weight:800;letter-spacing:-0.03em;
  background:var(--grad1);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header .subtitle{color:var(--text2);font-size:1rem;margin-top:10px;font-weight:400}
.header .timestamp{color:var(--text3);font-size:0.8rem;margin-top:6px}

/* ── Hero ── */
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:36px 0}
.hero-card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:24px 16px;text-align:center;
  transition:all .25s ease;position:relative;overflow:hidden}
.hero-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--border);transition:background .25s}
.hero-card:hover{transform:translateY(-3px);border-color:var(--border2);box-shadow:var(--glow)}
.hero-card .value{font-size:2.2rem;font-weight:800;letter-spacing:-0.02em;
  font-family:'JetBrains Mono','SF Mono',monospace}
.hero-card .label{color:var(--text2);font-size:0.78rem;margin-top:6px;font-weight:500;
  text-transform:uppercase;letter-spacing:0.5px}
.hero-card.green::before{background:var(--grad2)}.hero-card.green .value{color:var(--green)}
.hero-card.accent::before{background:var(--grad1)}.hero-card.accent .value{color:var(--accent)}
.hero-card.purple::before{background:linear-gradient(90deg,var(--purple),var(--accent))}
.hero-card.purple .value{color:var(--purple)}
.hero-card.cyan::before{background:linear-gradient(90deg,var(--cyan),var(--green))}
.hero-card.cyan .value{color:var(--cyan)}
.hero-card.yellow::before{background:linear-gradient(90deg,var(--yellow),var(--orange))}
.hero-card.yellow .value{color:var(--yellow)}

/* ── Pipeline Flow ── */
.pipeline-flow{display:flex;align-items:center;justify-content:center;gap:0;
  margin:28px 0 8px;padding:16px 0;overflow-x:auto}
.pipe-step{display:flex;flex-direction:column;align-items:center;gap:6px;min-width:90px}
.pipe-dot{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-size:0.85rem;font-weight:700;border:2px solid var(--border2);
  background:var(--surface2);transition:all .2s}
.pipe-dot.active{border-color:var(--green);background:rgba(63,185,80,0.12);color:var(--green)}
.pipe-dot.pass{border-color:var(--green);background:rgba(63,185,80,0.15);color:var(--green)}
.pipe-dot.fail{border-color:var(--red);background:rgba(248,81,73,0.15);color:var(--red)}
.pipe-label{font-size:0.7rem;color:var(--text2);text-align:center;max-width:80px;line-height:1.3}
.pipe-arrow{width:32px;height:2px;background:var(--border2);flex-shrink:0;margin-bottom:20px}
.pipe-arrow.active{background:var(--green)}

/* ── Sections ── */
.section{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);margin:20px 0;overflow:hidden;
  transition:border-color .2s}
.section:hover{border-color:var(--border2)}
.section-header{padding:18px 24px;cursor:pointer;display:flex;
  align-items:center;justify-content:space-between;user-select:none;transition:background .15s}
.section-header:hover{background:var(--surface2)}
.section-header h2{font-size:1.05rem;font-weight:600;display:flex;align-items:center;gap:10px;
  letter-spacing:-0.01em}
.section-header .icon{font-size:1.15rem}
.section-header .chevron{transition:transform .25s ease;color:var(--text3);font-size:0.85rem}
.section-header.collapsed .chevron{transform:rotate(-90deg)}
.section-body{padding:4px 24px 24px}
.section-body.hidden{display:none}

/* ── Tables ── */
table{width:100%;border-collapse:separate;border-spacing:0;font-size:0.88rem}
th{text-align:left;padding:10px 14px;color:var(--text3);font-weight:600;
  border-bottom:1px solid var(--border2);font-size:0.75rem;text-transform:uppercase;letter-spacing:0.8px}
td{padding:11px 14px;border-bottom:1px solid var(--border)}
tr:hover td{background:rgba(255,255,255,0.02)}
.mono{font-family:'JetBrains Mono','SF Mono',Consolas,monospace;font-size:0.82rem}
.right{text-align:right}
.pass{color:var(--green);font-weight:600}
.fail{color:var(--red);font-weight:600}
.best{color:var(--green);font-weight:700}

/* ── Badges ── */
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:0.72rem;font-weight:600;
  letter-spacing:0.3px}
.badge-green{background:rgba(63,185,80,0.12);color:var(--green);border:1px solid rgba(63,185,80,0.2)}
.badge-red{background:rgba(248,81,73,0.1);color:var(--red);border:1px solid rgba(248,81,73,0.2)}
.badge-yellow{background:rgba(210,153,34,0.1);color:var(--yellow);border:1px solid rgba(210,153,34,0.2)}
.badge-accent{background:rgba(88,166,255,0.1);color:var(--accent);border:1px solid rgba(88,166,255,0.2)}
.badge-purple{background:rgba(188,140,255,0.1);color:var(--purple);border:1px solid rgba(188,140,255,0.2)}

/* ── Bars ── */
.bar-row{display:flex;align-items:center;gap:14px;margin:8px 0}
.bar-label{width:150px;font-size:0.82rem;color:var(--text2);text-align:right;flex-shrink:0;font-weight:500}
.bar-track{flex:1;height:28px;background:var(--surface2);border-radius:6px;overflow:hidden;position:relative}
.bar-fill{height:100%;border-radius:6px;transition:width .8s cubic-bezier(.4,0,.2,1);min-width:3px}
.bar-value{position:absolute;right:10px;top:50%;transform:translateY(-50%);
  font-size:0.73rem;font-weight:600;color:var(--text);font-family:'JetBrains Mono',monospace}

/* ── Decision Grid ── */
.decision-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.decision-card{background:var(--surface2);border-radius:var(--radius-sm);padding:14px 16px;
  border-left:3px solid var(--border);transition:all .15s}
.decision-card:hover{background:var(--surface3)}
.decision-card.keep{border-left-color:var(--green)}
.decision-card.remove{border-left-color:var(--red)}
.decision-card .name{font-weight:600;font-size:0.88rem}
.decision-card .status{font-size:0.78rem;margin-top:3px;font-weight:600}
.decision-card.keep .status{color:var(--green)}
.decision-card.remove .status{color:var(--red)}
.decision-card .reason{color:var(--text3);font-size:0.78rem;margin-top:4px}

/* ── Recommendations ── */
.rec-item{display:flex;align-items:flex-start;gap:14px;padding:14px 0;
  border-bottom:1px solid var(--border)}
.rec-item:last-child{border-bottom:none}
.rec-rank{width:30px;height:30px;border-radius:50%;
  background:linear-gradient(135deg,rgba(88,166,255,0.15),rgba(188,140,255,0.15));
  border:1px solid rgba(88,166,255,0.2);
  display:flex;align-items:center;justify-content:center;font-weight:700;
  font-size:0.82rem;color:var(--accent);flex-shrink:0}
.rec-text{font-size:0.88rem;line-height:1.5}

/* ── Patterns ── */
.pattern-item{background:var(--surface2);border-radius:var(--radius-sm);padding:14px 18px;
  margin:8px 0;border:1px solid var(--border);transition:border-color .15s}
.pattern-item:hover{border-color:var(--border2)}
.pattern-ops{font-family:'JetBrains Mono',monospace;font-size:0.85rem;color:var(--cyan);font-weight:500}
.pattern-meta{display:flex;flex-wrap:wrap;gap:16px;margin-top:8px;font-size:0.78rem;color:var(--text2)}

/* ── Register Heatmap ── */
.reg-grid{display:grid;grid-template-columns:repeat(16,1fr);gap:4px;margin:12px 0}
.reg-cell{aspect-ratio:1;border-radius:4px;display:flex;align-items:center;justify-content:center;
  font-size:0.65rem;font-family:'JetBrains Mono',monospace;font-weight:600;
  border:1px solid var(--border);transition:all .15s}
.reg-cell:hover{transform:scale(1.15);z-index:1}
.reg-used{background:rgba(63,185,80,0.15);border-color:rgba(63,185,80,0.3);color:var(--green)}
.reg-unused{background:rgba(248,81,73,0.08);border-color:rgba(248,81,73,0.15);color:var(--text3)}

/* ── Donut Chart ── */
.donut-wrap{display:flex;align-items:center;gap:32px;margin:16px 0}
.donut{width:160px;height:160px;border-radius:50%;position:relative;flex-shrink:0}
.donut-center{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  text-align:center}
.donut-center .big{font-size:1.6rem;font-weight:800;font-family:'JetBrains Mono',monospace}
.donut-center .small{font-size:0.7rem;color:var(--text2);margin-top:2px}
.donut-legend{display:flex;flex-direction:column;gap:8px}
.legend-item{display:flex;align-items:center;gap:8px;font-size:0.82rem}
.legend-dot{width:10px;height:10px;border-radius:3px;flex-shrink:0}

/* ── Sweep Table ── */
.sweep-table{width:100%;border-collapse:separate;border-spacing:0 4px}
.sweep-table td{padding:8px 12px;background:var(--surface2);font-size:0.85rem}
.sweep-table tr td:first-child{border-radius:8px 0 0 8px}
.sweep-table tr td:last-child{border-radius:0 8px 8px 0}
.sweep-bar-cell{position:relative;min-width:120px}
.sweep-bar-bg{height:22px;border-radius:4px;transition:width .6s ease}

/* ── Footer ── */
.footer{text-align:center;padding:40px 0 24px;color:var(--text3);font-size:0.78rem;
  border-top:1px solid var(--border);margin-top:32px}

/* ── Animations ── */
@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
.section{animation:fadeUp .4s ease both}
.section:nth-child(2){animation-delay:.05s}
.section:nth-child(3){animation-delay:.1s}
.section:nth-child(4){animation-delay:.15s}

/* ── Print ── */
@media print{
  .nav,.pipe-arrow{display:none}
  body{background:#fff;color:#000;padding-top:0}
  .section,.hero-card{border:1px solid #ddd;break-inside:avoid}
  .hero-card .value{-webkit-text-fill-color:initial}
  .header h1{-webkit-text-fill-color:initial;color:#333}
}
"""

JS = r"""
document.querySelectorAll('.section-header').forEach(h=>{
  h.addEventListener('click',()=>{
    h.classList.toggle('collapsed');
    h.nextElementSibling.classList.toggle('hidden');
  });
});
/* Animate bars on scroll */
const obs=new IntersectionObserver(entries=>{
  entries.forEach(e=>{if(e.isIntersecting){
    e.target.querySelectorAll('.bar-fill').forEach(b=>{b.style.width=b.dataset.w});
    e.target.querySelectorAll('.sweep-bar-bg').forEach(b=>{b.style.width=b.dataset.w});
    obs.unobserve(e.target);
  }});
},{threshold:0.2});
document.querySelectorAll('.section').forEach(s=>obs.observe(s));
/* Smooth scroll nav */
document.querySelectorAll('.nav-link').forEach(a=>{
  a.addEventListener('click',e=>{
    e.preventDefault();
    const t=document.querySelector(a.getAttribute('href'));
    if(t)t.scrollIntoView({behavior:'smooth',block:'start'});
  });
});
/* Counter animation */
document.querySelectorAll('[data-count]').forEach(el=>{
  const end=parseFloat(el.dataset.count);
  const suffix=el.dataset.suffix||'';
  const decimals=el.dataset.decimals||0;
  let start=0;const dur=800;const t0=performance.now();
  function tick(now){
    const p=Math.min((now-t0)/dur,1);
    const ease=1-Math.pow(1-p,3);
    const v=start+(end-start)*ease;
    el.textContent=decimals>0?v.toFixed(decimals)+suffix:Math.round(v).toLocaleString()+suffix;
    if(p<1)requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
});
"""
