// AIRI 流水线阶段指示器（补丁文件，构建后需复制进 dist/ 并在 index.html 加 script 引用）
// 监听 app 内 dispatch 的 airi:stage 事件，显示各阶段耗时。
// 面板可拖拽（标题栏）、可关闭（✕，持久化），关闭后右下角出现 ⏱ 圆点可恢复。
(function () {
  if (window.__stageMonitorInstalled) return
  window.__stageMonitorInstalled = true

  // 不用 emoji（Windows 上缺彩色字体，渲染成丑字形）：状态色点 + 彩色短标签
  var ICONS = { asr: '识别', llm: '大模型', tts: '合成' }
  var history = []
  var roundStart = 0
  var current = null

  var HIDDEN_KEY = 'settings/stage-monitor/panel-hidden'
  var POS_KEY = 'settings/stage-monitor/panel-pos'
  var hidden = localStorage.getItem(HIDDEN_KEY) === '1'

  // ---- container (drag + close chrome) ------------------------------------
  var panel = document.createElement('div')
  panel.id = 'stage-monitor'
  var savedPos = null
  try { savedPos = JSON.parse(localStorage.getItem(POS_KEY) || 'null') } catch (e) { /* ignore */ }
  var baseCss = 'position:fixed;z-index:99999;width:300px;'
    + 'background:rgba(18,18,24,.9);color:#eee;border-radius:10px;'
    + 'font:12px/1.7 Consolas,"Microsoft YaHei",monospace;'
    + 'box-shadow:0 2px 10px rgba(0,0,0,.4);display:none;'
  if (savedPos && Number.isFinite(savedPos.left) && Number.isFinite(savedPos.top))
    panel.style.cssText = baseCss + 'left:' + savedPos.left + 'px;top:' + savedPos.top + 'px;'
  else
    panel.style.cssText = baseCss + 'top:8px;right:8px;'

  var header = document.createElement('div')
  header.style.cssText = 'display:flex;align-items:center;gap:6px;padding:4px 8px;cursor:move;'
    + 'user-select:none;background:rgba(255,255,255,.06);border-radius:10px 10px 0 0;touch-action:none;'
  var handle = document.createElement('span')
  handle.textContent = '≡ 流水线'
  handle.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#7dd3fc;'
  var closeBtn = document.createElement('button')
  closeBtn.textContent = '×'
  closeBtn.title = '隐藏'
  closeBtn.style.cssText = 'border:0;border-radius:6px;padding:0 7px;background:rgba(255,255,255,.12);'
    + 'color:inherit;font:inherit;cursor:pointer;line-height:20px;'
  closeBtn.onmouseenter = function () { closeBtn.style.background = 'rgba(255,255,255,.25)' }
  closeBtn.onmouseleave = function () { closeBtn.style.background = 'rgba(255,255,255,.12)' }
  closeBtn.onclick = function (e) {
    e.stopPropagation()
    hidden = true
    localStorage.setItem(HIDDEN_KEY, '1')
    render()
  }
  header.appendChild(handle)
  header.appendChild(closeBtn)

  var body = document.createElement('div')
  body.style.cssText = 'padding:6px 10px 8px;'

  panel.appendChild(header)
  panel.appendChild(body)

  // drag logic (pointer-based, like the CC panel)
  header.addEventListener('pointerdown', function (e) {
    if (e.target === closeBtn) return
    var rect = panel.getBoundingClientRect()
    var offsetX = e.clientX - rect.left
    var offsetY = e.clientY - rect.top
    function move(ev) {
      var left = Math.min(Math.max(0, ev.clientX - offsetX), window.innerWidth - rect.width)
      var top = Math.min(Math.max(0, ev.clientY - offsetY), window.innerHeight - 24)
      panel.style.left = left + 'px'
      panel.style.top = top + 'px'
      panel.style.right = 'auto'
    }
    function up() {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      var r = panel.getBoundingClientRect()
      localStorage.setItem(POS_KEY, JSON.stringify({ left: Math.round(r.left), top: Math.round(r.top) }))
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  })

  // ---- reopen dot ----------------------------------------------------------
  var dot = document.createElement('button')
  dot.textContent = '≡'
  dot.title = '打开流水线面板'
  dot.style.cssText = 'position:fixed;right:16px;bottom:60px;z-index:99999;width:36px;height:36px;'
    + 'border:0;border-radius:50%;background:rgba(18,18,24,.85);color:#7dd3fc;cursor:pointer;'
    + 'font-size:14px;box-shadow:0 4px 14px rgba(0,0,0,.4);display:none;'
  dot.onclick = function () {
    hidden = false
    localStorage.setItem(HIDDEN_KEY, '0')
    render()
  }

  // ---- service status (bat 启动的各服务实时状态) ------------------------------
  // 各服务 /health 都开了 CORS 且返回 JSON：status=ok 且模型已加载才算就绪；
  // 端口通但模型未载完显示"启动中"；连接被拒（服务未起）显示 down。
  var SERVICES = [
    { id: 'asr', name: '识别 8931', color: '#7dd3fc', url: 'http://localhost:8931/health',
      ready: function (j) { return j && j.status === 'ok' && j.model_loaded === true } },
    { id: 'tts', name: '合成 8930', color: '#c084fc', url: 'http://localhost:8930/health',
      ready: function (j) { return j && j.status === 'ok' && j.model_loaded === true } },
    { id: 'cc', name: '小K 8932', color: '#4ade80', url: 'http://localhost:8932/health',
      ready: function (j) { return j && j.status === 'ok' } },
    // LLM 不走 /health：读 fetch 护栏记录的真实请求结果（config-restore.js 写入）
    { id: 'llm', name: 'LLM', color: '#facc15', llm: true, pendingTag: '未验证', downTag: '请求失败' },
  ]
  var svcStatus = {}   // id -> 'checking' | 'up' | 'starting' | 'down'

  function checkLlmStatus() {
    var last = window.__AIRI_LLM_LAST
    if (!last)
      svcStatus.llm = 'checking'          // 本会话还没发过 LLM 请求
    else if (Date.now() - last.t > 10 * 60 * 1000)
      svcStatus.llm = 'checking'          // 超过 10 分钟无请求，状态过期
    else
      svcStatus.llm = last.ok ? 'up' : 'down'
  }

  function checkServices() {
    checkLlmStatus()
    SERVICES.forEach(function (s) {
      if (s.llm) return
      var ctl = new AbortController()
      var t0 = performance.now()
      var timer = setTimeout(function () { ctl.abort() }, 2500)
      fetch(s.url, { signal: ctl.signal })
        .then(function (r) { return r.json().catch(function () { return null }) })
        .then(function (j) {
          s._ms = Math.round(performance.now() - t0)
          svcStatus[s.id] = s.ready(j) ? 'up' : 'starting'
        })
        .catch(function () { svcStatus[s.id] = 'down'; s._ms = null })
        .finally(function () {
          clearTimeout(timer)
          render()
        })
    })
  }

  function renderServices() {
    var html = '<div style="display:flex;flex-wrap:wrap;gap:4px 10px;padding-bottom:5px;'
      + 'margin-bottom:5px;border-bottom:1px solid rgba(255,255,255,.12)">'
    SERVICES.forEach(function (s) {
      var st = svcStatus[s.id]
      var dot = st === 'up' ? '<span style="color:#4ade80">●</span>'
        : st === 'down' ? '<span style="color:#f87171">●</span>'
          : '<span style="color:#facc15">●</span>'
      var tag = st === 'up' ? '' : st === 'down' ? ' <span style="color:#f87171">' + (s.downTag || '未启动') + '</span>'
        : ' <span style="color:#facc15">' + (s.pendingTag || '启动中') + '</span>'
      var ms = st === 'up' && s._ms != null ? ' <span style="color:#888">' + s._ms + 'ms</span>' : ''
      html += '<span style="white-space:nowrap">' + dot + ' <span style="color:' + (s.color || '#eee') + '">' + esc(s.name) + '</span>' + tag + ms + '</span>'
    })
    html += '</div>'
    var p = window.__AIRI_TTS__
    if (p) {
      var flag = (p.req || 0) > 0 ? '#4ade80' : ((p.tokens || 0) > 0 ? '#facc15' : '#888')
      html += '<div style="font-size:10px;color:' + flag + ';padding-bottom:2px">TTS 探测 host=' + (p.host ? 'T' : 'F')
        + ' 会话=' + (p.sessions || 0) + (p.openErr ? ' 开错=' + p.openErr : '')
        + ' 令牌=' + (p.tokens || 0) + ' 回调=' + (p.ttsCalls || 0)
        + ' 实发=' + (p.req || 0) + (p.stage ? ' <span style="color:#7dd3fc">' + esc(p.stage) + '</span>' : '') + (p.drop ? ' <span style="color:#f87171">丢:' + esc(p.drop) + '</span>' : '') + '</div>'
      if (p.ends != null)
        html += '<div style="font-size:10px;color:#7dd3fc;padding-bottom:2px">回合完=' + p.ends
          + (p.endTxt ? ' 回复:' + esc(p.endTxt) : '') + '</div>'
    }
    var v = window.__AIRI_VOICE
    if (v)
      html += '<div style="font-size:10px;color:#c084fc;padding-bottom:3px">语音入口 收=' + (v.recv || 0)
        + (v.empty ? ' 空=' + v.empty : '') + (v.noModel ? ' <span style="color:#f87171">无模型=' + v.noModel + '</span>' : '')
        + ' 发=' + (v.sent || 0) + (v.err ? ' <span style="color:#f87171">错=' + v.err + '</span>' : '') + '</div>'
    return html
  }

  // ---- render --------------------------------------------------------------
  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;') }

  function render() {
    var html = renderServices()
    if (current)
      html += '<div style="font-size:14px;font-weight:bold;color:#7dd3fc">▶ ' + esc(current) + '</div>'
    for (var i = history.length - 1; i >= 0 && i >= history.length - 9; i--)
      html += '<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(history[i]) + '</div>'
    body.innerHTML = html
    var active = true   // 服务状态区常驻显示（启动阶段也要看得见）
    panel.style.display = (!hidden && active) ? 'block' : 'none'
    dot.style.display = hidden ? 'block' : 'none'
  }

  window.addEventListener('airi:stage', function (e) {
    var d = e.detail || {}
    var label = ICONS[d.stage] || d.stage || '?'
    var t = new Date().toTimeString().slice(0, 8)
    var note = d.note ? '「' + d.note + '」' : ''
    if (d.phase === 'start') {
      if (d.stage === 'asr') roundStart = performance.now()
      current = label + note
    }
    else if (d.phase === 'first') {
      history.push(t + ' ' + label + ' 首字 ' + (d.ms / 1000).toFixed(1) + 's')
      current = label + '（流式输出中）'
    }
    else if (d.phase === 'done') {
      history.push(t + ' ' + label + ' 用时 ' + (d.ms / 1000).toFixed(1) + 's' + note)
      current = null
      if (d.stage === 'tts' && roundStart) {
        history.push('—— 全程 ' + ((performance.now() - roundStart) / 1000).toFixed(1) + 's')
        roundStart = 0
      }
    }
    if (history.length > 60) history = history.slice(-60)
    render()
  })

  ;(document.documentElement || document).appendChild(panel)
  ;(document.documentElement || document).appendChild(dot)
  checkServices()
  setInterval(checkServices, 4000)
  render()
})();