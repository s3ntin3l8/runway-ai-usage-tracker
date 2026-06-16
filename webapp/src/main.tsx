import '@fontsource-variable/inter/index.css';
import '@fontsource-variable/jetbrains-mono/index.css';
import './styles/index.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { registerSW } from 'virtual:pwa-register';
import { App } from './app';

// Register the PWA service worker. This module is bundled into a hashed
// /assets script, so it satisfies the server's strict script-src 'self' CSP
// (no inline registration). autoUpdate (vite.config.ts) makes new versions
// activate on the next navigation.
registerSW({ immediate: true });

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
