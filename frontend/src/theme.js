// Runtime light/dark theme switching for PrimeReact.
//
// Genuine "Aura" theming is token-based and only ships in PrimeReact v11 (alpha).
// On stable PrimeReact 10.x we use Lara — Aura's direct design predecessor, the
// same PrimeTek lineage and visually near-identical. We import both the light and
// dark Lara theme CSS as URLs (Vite emits them as assets) and swap the <link>
// href at runtime so the toggle is instant with no rebuild.
import laraLight from 'primereact/resources/themes/lara-light-blue/theme.css?url'
import laraDark from 'primereact/resources/themes/lara-dark-blue/theme.css?url'

export function applyTheme(dark) {
  let link = document.getElementById('prime-theme')
  if (!link) {
    link = document.createElement('link')
    link.id = 'prime-theme'
    link.rel = 'stylesheet'
    document.head.appendChild(link)
  }
  link.href = dark ? laraDark : laraLight
  document.documentElement.classList.toggle('dark', dark)
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
}
