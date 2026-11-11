// M3 水波纹：在按下的位置生成一个圆，扩散到盖住整个控件，松手后淡出。
const SELECTOR = '.btn, .icon-btn, .seg-item, .nav-item, .canvas-context-menu button, .settings-category-row'
const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')

function rippleHost(target) {
  // 收起的导航栏只在指示块里出水波纹，展开后指示块只剩图标大小，改用整行。
  const indicator = target.querySelector(':scope > .nav-ind')
  return indicator && indicator.offsetWidth >= 48 ? indicator : target
}

function startRipple(event) {
  if (event.button !== 0 || reducedMotion?.matches || typeof Element.prototype.animate !== 'function') return
  const target = event.target.closest?.(SELECTOR)
  if (!target || target.disabled || target.getAttribute('aria-disabled') === 'true') return
  const host = rippleHost(target)
  const rect = host.getBoundingClientRect()
  const x = event.clientX - rect.left
  const y = event.clientY - rect.top
  const radius = Math.hypot(Math.max(x, rect.width - x), Math.max(y, rect.height - y))
  const ripple = document.createElement('span')
  ripple.className = 'md-ripple'
  ripple.style.width = ripple.style.height = `${radius * 2}px`
  ripple.style.left = `${x - radius}px`
  ripple.style.top = `${y - radius}px`
  host.appendChild(ripple)
  const pressedAt = performance.now()
  ripple.animate([{ transform: 'scale(.2)' }, { transform: 'scale(1)' }], {
    duration: 450, easing: 'cubic-bezier(.2,0,0,1)', fill: 'forwards',
  })
  const release = () => {
    window.removeEventListener('pointerup', release)
    window.removeEventListener('pointercancel', release)
    // 快速点一下也要让水波纹先扩散一段再淡出，不然只闪一下看不出来。
    const wait = Math.max(0, 225 - (performance.now() - pressedAt))
    setTimeout(() => {
      ripple.animate([{ opacity: 0.1 }, { opacity: 0 }], { duration: 150, easing: 'linear', fill: 'forwards' })
        .finished.then(() => ripple.remove(), () => ripple.remove())
    }, wait)
  }
  window.addEventListener('pointerup', release)
  window.addEventListener('pointercancel', release)
}

export function installRipple() {
  document.addEventListener('pointerdown', startRipple, { passive: true })
}
