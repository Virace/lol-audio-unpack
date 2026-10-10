import type { Theme } from 'vitepress'
import { inBrowser } from 'vitepress'
import DefaultTheme from 'vitepress/theme'
import { h } from 'vue'
import ReleaseInfo from './ReleaseInfo.vue'
import './custom.css'

/** 页面切换时淡入首页或正文；页内锚点跳转保持原有滚动行为。 */
function animatePage() {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  document.querySelector<HTMLElement>('.VPDoc .content-container, .VPHome, .NotFound')?.animate(
    [{ opacity: 0.72 }, { opacity: 1 }],
    { duration: 220, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' },
  )
}

export default {
  extends: DefaultTheme,
  Layout: () => h(DefaultTheme.Layout, null, {
    'home-hero-actions-after': () => h(ReleaseInfo),
  }),
  enhanceApp({ router }) {
    if (!inBrowser) return
    let currentPath = window.location.pathname
    router.onAfterRouteChange = (href) => {
      const nextPath = new URL(href, window.location.origin).pathname
      if (nextPath === currentPath) return
      currentPath = nextPath
      requestAnimationFrame(animatePage)
    }
  },
} satisfies Theme
