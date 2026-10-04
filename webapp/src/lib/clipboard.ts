// Copy text to the clipboard. `navigator.clipboard` only exists in a secure context, and a
// self-hosted Runway opened over `http://<lan-ip>` isn't one, so fall back to a hidden textarea
// and `execCommand('copy')`. Resolves false when both fail so the caller can show the text.

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // blocked by permissions — try the fallback
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
