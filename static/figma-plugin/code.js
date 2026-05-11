figma.showUI(__html__, { width: 340, height: 560 });

figma.clientStorage.getAsync('settings').then(settings => {
  figma.ui.postMessage({ type: 'init', settings: settings || null });
});

figma.ui.onmessage = async (msg) => {
  if (msg.type === 'close') { figma.closePlugin(); return; }

  if (msg.type === 'setStorage') {
    await figma.clientStorage.setAsync(msg.key, msg.value);
    return;
  }

  if (msg.type !== 'import') return;

  const { sections, analysis } = msg;

  const PAGE_PAD = 100;
  const SECTION_GAP = 80;
  const CARD_W = 390;
  const CARD_H = 844;
  const CARD_GAP = 24;
  const LABEL_H = 52;
  const SECTION_PAD = 40;
  const ANALYSIS_W = 480;

  await figma.loadFontAsync({ family: 'Inter', style: 'Regular' });
  await figma.loadFontAsync({ family: 'Inter', style: 'Bold' });

  let sectionX = PAGE_PAD;

  if (analysis) {
    const defs = [
      { key: 'domain_analysis',          label: 'DOMAIN & POSITIONING' },
      { key: 'monetization_hypothesis',  label: 'MONETIZATION HYPOTHESIS' },
      { key: 'onboarding_hypothesis',    label: 'ONBOARDING HYPOTHESIS' },
      { key: 'feature_strategy_reasoning', label: 'FEATURE STRATEGY' },
      { key: 'copy_and_cta',             label: 'COPY & CTA' },
      { key: 'product_bets',             label: 'PRODUCT BETS & A/B HYPOTHESES' },
    ];

    let ty = PAGE_PAD;

    const title = figma.createText();
    title.fontName = { family: 'Inter', style: 'Bold' };
    title.characters = 'UX ANALYSIS';
    title.fontSize = 13;
    title.fills = [{ type: 'SOLID', color: { r: 0.05, g: 0.05, b: 0.05 } }];
    title.x = 0;
    title.y = ty;
    figma.currentPage.appendChild(title);
    ty += title.height + 20;

    for (const { key, label } of defs) {
      const text = (analysis[key] || '').trim();
      if (!text || text === 'Not observed.') continue;

      const h = figma.createText();
      h.fontName = { family: 'Inter', style: 'Bold' };
      h.characters = label;
      h.fontSize = 11;
      h.fills = [{ type: 'SOLID', color: { r: 0.1, g: 0.1, b: 0.1 } }];
      h.x = 0;
      h.y = ty;
      figma.currentPage.appendChild(h);
      ty += h.height + 6;

      const b = figma.createText();
      b.fontName = { family: 'Inter', style: 'Regular' };
      b.fontSize = 11;
      b.textAutoResize = 'HEIGHT';
      b.resize(ANALYSIS_W, 20);
      b.characters = text;
      b.fills = [{ type: 'SOLID', color: { r: 0.45, g: 0.45, b: 0.45 } }];
      b.x = 0;
      b.y = ty;
      figma.currentPage.appendChild(b);
      ty += b.height + 24;
    }

    sectionX = ANALYSIS_W + SECTION_GAP;
  }
  const created = [];

  for (const sec of sections) {
    const count = sec.images.length;
    if (count === 0) continue;

    const cols = count;
    const rows = 1;
    const innerW = cols * (CARD_W + CARD_GAP) - CARD_GAP;
    const innerH = rows * (CARD_H + CARD_GAP + LABEL_H) - CARD_GAP;
    const sectionW = innerW + SECTION_PAD * 2;
    const sectionH = innerH + SECTION_PAD * 2;

    const section = figma.createSection();
    section.name = sec.name;
    section.x = sectionX;
    section.y = PAGE_PAD;
    section.resizeWithoutConstraints(sectionW, sectionH);
    figma.currentPage.appendChild(section);
    created.push(section);

    for (let i = 0; i < count; i++) {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const x = SECTION_PAD + col * (CARD_W + CARD_GAP);
      const y = SECTION_PAD + row * (CARD_H + CARD_GAP + LABEL_H);

      const { bytes, label, key_text, components = [], state = '' } = sec.images[i];

      if (bytes.length > 0) {
        const imageHash = figma.createImage(new Uint8Array(bytes)).hash;
        const rect = figma.createRectangle();
        rect.resize(CARD_W, CARD_H);
        rect.x = x;
        rect.y = y;
        rect.cornerRadius = 16;
        rect.fills = [{ type: 'IMAGE', imageHash, scaleMode: 'FILL' }];
        section.appendChild(rect);
      }

      if (key_text) {
        const kt = figma.createText();
        kt.fontName = { family: 'Inter', style: 'Regular' };
        kt.characters = key_text;
        kt.fontSize = 12;
        kt.fills = [{ type: 'SOLID', color: { r: 0.1, g: 0.1, b: 0.1 } }];
        kt.x = x;
        kt.y = y + CARD_H + 6;
        section.appendChild(kt);
      }

      if (components.length > 0) {
        const tags = figma.createText();
        tags.fontName = { family: 'Inter', style: 'Regular' };
        tags.characters = components.join('  ·  ');
        tags.fontSize = 10;
        tags.fills = [{ type: 'SOLID', color: { r: 0.6, g: 0.4, b: 0.9 } }];
        tags.x = x;
        tags.y = y + CARD_H + 22;
        section.appendChild(tags);
      }

      if (state) {
        const stateLabel = figma.createText();
        stateLabel.fontName = { family: 'Inter', style: 'Regular' };
        stateLabel.characters = state;
        stateLabel.fontSize = 10;
        stateLabel.fills = [{ type: 'SOLID', color: { r: 0.9, g: 0.5, b: 0.2 } }];
        stateLabel.x = x;
        stateLabel.y = y + CARD_H + 38;
        section.appendChild(stateLabel);
      }
    }

    sectionX += sectionW + SECTION_GAP;
  }

  figma.viewport.scrollAndZoomIntoView(created);
  const total = sections.reduce((n, s) => n + s.images.length, 0);
  figma.notify(`Imported ${total} screens across ${created.length} sections`);
  figma.ui.postMessage({ type: 'done', total, sections: created.length });
};
