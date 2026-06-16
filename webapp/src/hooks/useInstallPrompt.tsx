import { createContext, useCallback, useContext, useEffect, useState } from 'react';

// The beforeinstallprompt event isn't in the standard DOM lib types.
interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

interface InstallContextValue {
  /** A native install prompt is available (Android / desktop Chromium). */
  canInstall: boolean;
  /** Trigger the browser's install prompt. No-op if unavailable. */
  promptInstall: () => Promise<void>;
  /** iOS Safari, which has no install API — show an "Add to Home Screen" hint. */
  isIOS: boolean;
  /** Already running as an installed standalone app. */
  isStandalone: boolean;
}

const InstallContext = createContext<InstallContextValue | null>(null);

function detectIOS(): boolean {
  const ua = navigator.userAgent;
  // iPadOS 13+ reports as a Mac, so fall back to touch-point detection.
  const isAppleMobile =
    /iPad|iPhone|iPod/.test(ua) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  // Add-to-Home-Screen only exists in Safari, not iOS Chrome/Firefox/Edge.
  const isSafari = /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS/.test(ua);
  return isAppleMobile && isSafari;
}

function detectStandalone(): boolean {
  return (
    window.matchMedia?.('(display-mode: standalone)').matches ||
    // iOS Safari legacy flag.
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

export function InstallProvider({ children }: { children: React.ReactNode }) {
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null);
  const [isStandalone, setIsStandalone] = useState(detectStandalone);
  const [isIOS] = useState(detectIOS);

  useEffect(() => {
    const onBeforeInstall = (e: Event) => {
      // Suppress the mini-infobar; we surface install via our own UI instead.
      e.preventDefault();
      setDeferred(e as BeforeInstallPromptEvent);
    };
    const onInstalled = () => {
      setDeferred(null);
      setIsStandalone(true);
    };
    window.addEventListener('beforeinstallprompt', onBeforeInstall);
    window.addEventListener('appinstalled', onInstalled);
    return () => {
      window.removeEventListener('beforeinstallprompt', onBeforeInstall);
      window.removeEventListener('appinstalled', onInstalled);
    };
  }, []);

  const promptInstall = useCallback(async () => {
    if (!deferred) return;
    await deferred.prompt();
    await deferred.userChoice;
    // The prompt event can only be used once.
    setDeferred(null);
  }, [deferred]);

  const value: InstallContextValue = {
    canInstall: deferred !== null && !isStandalone,
    promptInstall,
    isIOS,
    isStandalone,
  };

  return <InstallContext.Provider value={value}>{children}</InstallContext.Provider>;
}

export function useInstallPrompt(): InstallContextValue {
  const ctx = useContext(InstallContext);
  if (!ctx) throw new Error('useInstallPrompt must be used inside <InstallProvider>');
  return ctx;
}
