// AIRI 流水线阶段指示器（补丁文件，构建后需复制进 dist/ 并在 index.html 加 script 引用）
// 监听 app 内 dispatch 的 airi:stage 事件，在页面右上角显示各阶段耗时
(function () {
  if (window.__stageMonitorInstalled) return
  window.__stageMonitorInstalled = true

  var ICONS = { asr: '🎤 识别', llm: '🤔 大模型', tts: '🔊 合成' }
  var history = []
  var roundStart = 0
  var current = null
  var panel = document.createElement('div')
  panel.id = 'stage-monitor'
  panel.style.cssText = 'position:fixed;top:8px;right:8px;z-index:99999;width:300px;'
    + 'background:rgba(18,18,24,.9);color:#eee;border-radius:10px;padding:8px 10px;'
    + 'font:12px/1.7 Consolas,"Microsoft YaHei",monospace;pointer-events:none;'
    + 'box-shadow:0 2px 10px rgba(0,0,0,.4);display:none;'

  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;') }

  function render() {
    var html = ''
    if (current)
      html += '<div style="font-size:14px;font-weight:bold;color:#7dd3fc">▶ ' + esc(current) + '</div>'
    for (var i = history.length - 1; i >= 0 && i >= history.length - 9; i--)
      html += '<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(history[i]) + '</div>'
    panel.innerHTML = html
    panel.style.display = (history.length || current) ? 'block' : 'none'
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
})();
