// App hooks for visual-kit (CONTRACT.md "visual/scenes.json"). They run inside the visual-kit image and reach the
// demo over HTTP only. Runway has no login, and its theme follows the browser's colour scheme, so all that is
// left from the capture script this replaced is the readiness logic: wait for the skeletons, dismiss the setup
// banners a demo always shows, and let the charts finish drawing.

// A scene's click steps leave the clicked control (a tab, say) focused, and the app's focus ring is painted on it. A
// user's mouse click does not show that ring, so drop focus again after every click. Added to the context, so it
// also covers the loop recording, whose steps run after settle().
//
// The charts are ECharts canvases: a legend or axis is laid out from text widths measured the moment the chart
// first renders. If the web font (Inter, JetBrains Mono) is still loading then, the widths are the fallback
// font's, the layout is never redone, and the shot differs from run to run. The charts render when the first
// API answers, so hold every fetch until the fonts are in.
export async function beforeScene({ context }) {
  await context.addInitScript(() => {
    document.addEventListener(
      "click",
      () => requestAnimationFrame(() => document.activeElement?.blur?.()),
      true,
    );

    const sample = "AaBb0123456789 .,:%$~+-\u00b7";
    const faces = ['16px "Inter Variable"', '16px "JetBrains Mono Variable"'];
    const fontsLoaded = () =>
      Promise.all(faces.map((face) => document.fonts.load(face, sample))).then(
        () => document.fonts.ready,
      );
    const realFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      await fontsLoaded();
      return realFetch(...args);
    };
  });
}

export async function settle({ page, scene }) {
  // Skeletons (and the chart placeholders) shimmer until their query resolves. A busy host can leave them up
  // past network idle, so wait them out instead of shooting a half-loaded page.
  await page
    .waitForFunction(() => !document.querySelector(".animate-shimmer"), null, {
      timeout: 60_000,
      polling: 250,
    })
    .catch(() => {
      throw new Error(`${scene.name}: skeletons still shimmering after 60s`);
    });

  // Home banners nag about setup (no alert webhook in a demo): dismiss them the way a user would, so they do not
  // sit on top of every capture.
  for (const close of await page.getByRole("button", { name: /dismiss/i }).all()) {
    await close.click().catch(() => {});
  }
  await page.evaluate(() => document.fonts.ready);

  // Charts below the fold start drawing only once they are scrolled into view, and a full-page shot would catch
  // them mid-animation (or not drawn at all). Scroll the whole page once, then back to the top, so they have
  // all started before the page is judged steady.
  await page.evaluate(async () => {
    const step = Math.max(window.innerHeight / 2, 200);
    for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
      window.scrollTo(0, y);
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    window.scrollTo(0, 0);
  });

  // Chart entrance animations are JS-driven, so reduced motion does not stop them, and a dismissed banner moves
  // the page: wait until every element has held its box for 1.5 s.
  await page
    .evaluate(async () => {
      const boxes = () =>
        [...document.querySelectorAll("body *")]
          .map((e) => {
            const r = e.getBoundingClientRect();
            return [r.x, r.y, r.width, r.height].map((n) => n.toFixed(2)).join(",");
          })
          .join("|");
      let last = boxes();
      let steady = 0;
      for (let i = 0; i < 80; i++) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        const now = boxes();
        steady = now === last ? steady + 1 : 0;
        if (steady >= 6) return;
        last = now;
      }
      throw new Error("the page keeps moving");
    })
    .catch((err) => {
      throw new Error(`${scene.name}: ${err.message.split("\n")[0]}`);
    });

  // The charts are ECharts canvases, animated by the library itself: their drawing is invisible to the box check
  // above. Wait until every canvas has held the same pixels for 1.5 s.
  await page
    .evaluate(async () => {
      const pixels = () =>
        [...document.querySelectorAll("canvas")]
          .map((c) => {
            const data = c.toDataURL();
            let hash = 5381;
            for (let i = 0; i < data.length; i += 7) hash = ((hash << 5) + hash + data.charCodeAt(i)) | 0;
            return `${c.width}x${c.height}:${data.length}:${hash}`;
          })
          .join("|");
      let last = pixels();
      let steady = 0;
      for (let i = 0; i < 80; i++) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        const now = pixels();
        steady = now === last ? steady + 1 : 0;
        if (steady >= 6) return;
        last = now;
      }
      throw new Error("a chart keeps redrawing");
    })
    .catch((err) => {
      throw new Error(`${scene.name}: ${err.message.split("\n")[0]}`);
    });
}
