/* Keep secondary options in a paged dialog without growing the workbench. */
(() => {
  const form = document.querySelector('#settings-form');
  const dialog = document.createElement('dialog');
  dialog.id = 'settings-dialog';
  dialog.setAttribute('aria-labelledby', 'settings-title');
  dialog.innerHTML = `<div class="dialog-heading"><h2 id="settings-title"></h2><button type="button" class="icon-button" aria-label="关闭参数面板"><img class="icon" src="/assets/icons/x.svg" alt=""></button></div><div class="settings-pages"></div><div class="settings-footer"><div class="pager"><button type="button" class="glass-button previous" aria-label="上一页">上一页</button><span class="page-number" aria-live="polite"></span><button type="button" class="glass-button next" aria-label="下一页">下一页</button></div><div class="settings-actions"><button type="button" class="glass-button restore" hidden>恢复默认</button><button type="button" class="glass-button done">完成</button></div></div>`;
  document.body.append(dialog);
  const pagesHost = dialog.querySelector('.settings-pages');
  let owner, items = [], pages = [], page = 0, trigger;
  function render() {
    pages.forEach((el, i) => el.hidden = i !== page);
    dialog.querySelector('.page-number').textContent = `${page + 1} / ${pages.length}`;
    dialog.querySelector('.previous').disabled = page === 0;
    dialog.querySelector('.next').disabled = page === pages.length - 1;
    dialog.querySelector('.pager').hidden = pages.length <= 1;
  }
  function paginate() {
    if (!owner) return;
    const previousPage = page;
    pagesHost.replaceChildren(); pages = []; page = 0;
    const budget = Math.max(80, innerHeight - 190);
    let current;
    for (const item of items) {
      if (!current) { current = document.createElement('div'); current.className = 'settings-page'; pages.push(current); pagesHost.append(current); }
      current.append(item);
      if (current.getBoundingClientRect().height > budget && current.children.length > 1) {
        current = document.createElement('div'); current.className = 'settings-page'; pages.push(current); pagesHost.append(current); current.append(item);
      }
    }
    page = Math.min(previousPage, pages.length - 1);
    render();
  }
  function open(details) {
    if (dialog.open) return;
    owner = details;
    dialog.querySelector('.restore').hidden = !details.classList.contains('advanced');
    trigger = details.querySelector('summary');
    dialog.querySelector('h2').textContent = trigger.textContent.trim();
    const content = details.querySelector('.detail-content');
    // Keep the controls associated with their original form and event listeners.
    items = [...content.children];
    content.querySelectorAll('input,select,textarea').forEach(el => el.setAttribute('form', form.id));
    page = 0;
    dialog.showModal(); paginate();
    dialog.querySelector('.dialog-heading button').focus();
  }
  // Flatten only the advanced slider container, so each control can be paged.
  const sliders = document.querySelector('#advanced-sliders');
  sliders.replaceWith(...sliders.children);
  const sections = [...form.querySelectorAll('details')];
  const inspector = document.querySelector('.inspector');
  function restoreAdvanced(roots) {
    for (const item of roots) {
      for (const control of item.querySelectorAll('input,select')) {
        if (control.name === 'protect_region') control.value = '[]';
        else if (control.tagName === 'SELECT') control.selectedIndex = Math.max(0,[...control.options].findIndex(option=>option.defaultSelected));
        else if (control.type === 'checkbox') control.checked = control.defaultChecked;
        else control.value = control.defaultValue;
        control.dispatchEvent(new Event('input', {bubbles:true}));
      }
    }
  }
  const inlineRestore = document.createElement('div');
  inlineRestore.className = 'inline-restore';
  inlineRestore.innerHTML = '<button type="button" class="glass-button">恢复默认</button>';
  const advancedContent = form.querySelector('.advanced .detail-content');
  advancedContent.append(inlineRestore);
  inlineRestore.querySelector('button').onclick = () => restoreAdvanced([advancedContent]);
  sections.forEach(details => {
    details.open = false;
    details.querySelector('summary').addEventListener('click', event => {
      event.preventDefault();
      if (!details.classList.contains('inline-settings')) open(details);
    });
  });
  let layoutFrame;
  function scheduleLayout() {
    cancelAnimationFrame(layoutFrame);
    layoutFrame = requestAnimationFrame(fitSettings);
  }
  function fitSettings() {
    if (dialog.open) return;
    const focusedSection = document.activeElement?.closest('details');
    for (const details of sections) {
      details.classList.remove('inline-settings'); details.open = false;
      details.querySelector('summary').setAttribute('aria-haspopup', 'dialog');
    }
    if (!inspector.getClientRects().length) return;
    const bottom = inspector.getBoundingClientRect().bottom - parseFloat(getComputedStyle(inspector).paddingBottom) - 4;
    for (const details of sections) {
      if (!details.getClientRects().length || details.hasAttribute('data-collapsed')) continue;
      details.classList.add('inline-settings'); details.open = true;
      if (form.getBoundingClientRect().bottom > bottom) {
        details.classList.remove('inline-settings'); details.open = false;
      } else details.querySelector('summary').removeAttribute('aria-haspopup');
    }
    if (focusedSection && !focusedSection.open) focusedSection.querySelector('summary').focus();
  }
  new ResizeObserver(scheduleLayout).observe(inspector);
  new ResizeObserver(scheduleLayout).observe(form);
  new MutationObserver(scheduleLayout).observe(form, {subtree:true,attributes:true,attributeFilter:['hidden']});
  form.addEventListener('change', scheduleLayout);
  form.addEventListener('reset', scheduleLayout);
  window.addEventListener('resize', scheduleLayout);
  document.fonts.ready.then(scheduleLayout);
  scheduleLayout();
  dialog.addEventListener('close', () => {
    if (owner) owner.querySelector('.detail-content').append(...items);
    owner = null; items = []; pagesHost.replaceChildren(); trigger?.focus(); scheduleLayout();
  });
  dialog.querySelector('.dialog-heading button').onclick = () => dialog.close();
  dialog.querySelector('.restore').onclick = () => {
    if (!owner?.classList.contains('advanced')) return;
    restoreAdvanced(items);
  };
  dialog.querySelector('.done').onclick = () => dialog.close();
  dialog.querySelector('.previous').onclick = () => { page--; render(); };
  dialog.querySelector('.next').onclick = () => { page++; render(); };
  // Reflow when conditional codec / quality fields appear, or the window resizes.
  dialog.addEventListener('change', () => requestAnimationFrame(paginate));
  window.addEventListener('resize', paginate);
  document.addEventListener('invalid', event => {
    if (dialog.contains(event.target)) {
      page = pages.findIndex(p => p.contains(event.target)); render();
    } else {
      const details = event.target.closest('details');
      if (details && !details.classList.contains('inline-settings')) { open(details); page = pages.findIndex(p => p.contains(event.target)); render(); }
    }
  }, true);
  const settingsButton = document.createElement('button');
  settingsButton.type = 'button'; settingsButton.className = 'glass-button mobile-settings'; settingsButton.textContent = '增强设置';
  settingsButton.setAttribute('aria-expanded', 'false');
  document.querySelector('.canvas-toolbar').append(settingsButton);
  const close = document.createElement('button'); close.type = 'button'; close.className = 'icon-button inspector-close'; close.setAttribute('aria-label','收起增强设置'); close.innerHTML = '<img class="icon" src="/assets/icons/x.svg" alt="">';
  document.querySelector('.inspector-title').append(close);
  const background = [document.querySelector('.topbar'), ...document.querySelector('.workspace').children].filter(el => el !== inspector);
  function closeInspector() { inspector.classList.remove('mobile-open'); background.forEach(el => el.inert = false); settingsButton.setAttribute('aria-expanded','false'); settingsButton.focus(); }
  settingsButton.onclick = () => { inspector.classList.add('mobile-open'); background.forEach(el => el.inert = true); settingsButton.setAttribute('aria-expanded','true'); close.focus(); scheduleLayout(); };
  close.onclick = closeInspector;
  window.addEventListener('resize', () => { if (getComputedStyle(settingsButton).display === 'none' && inspector.classList.contains('mobile-open')) closeInspector(); });
  document.addEventListener('keydown', e => {
    if (dialog.open || !inspector.classList.contains('mobile-open')) return;
    if (e.key === 'Escape') closeInspector();
    if (e.key === 'Tab') {
      const controls = [...inspector.querySelectorAll('button,select,input,summary')].filter(el => !el.disabled && el.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
})();

// Native tooltips appear after the pointer rests, including controls moved into dialogs.
(() => {
  const help = {
    size: '保持原图比例适配所选分辨率；原始不改变尺寸。2K 为 2560×1440 档位。',
    intensity: '调低：画面变化更克制；调高：增强更明显。例如人像处理过重时调低，光影变化太弱时调高。',
    local_structure: '调低：局部结构改动更弱；调高：结构增强更强。例如观察衣物褶皱或建筑轮廓，过于生硬时调低。实际效果随素材变化。',
    local_tone: '调低：局部色调变化更少；调高：局部色调处理更强。例如观察面部或衣物的明暗层次，变化过重时调低。',
    skin: '调低：对皮肤结构的处理更克制；调高：处理更强。例如观察面颊纹理，出现不自然的皮肤细节时调低；不是美白或磨皮开关。',
    global_tone: '调低：整体色调改动更弱；调高：整体色调处理更强。例如整张照片的明暗或色调变化太大时调低；并非单纯调亮。',
    detail: '调低：更多保留增强前的画面；调高：更多采用增强结果。0% 为增强前画面，100% 为完整效果，超过 100% 会进一步放大差异，可能产生伪影。',
    color: '调低：更多保留原图色相；调高：更多采用增强后的颜色。例如衣服或肤色偏色时调低。0% 仍可保留亮度增强，100% 采用增强结果的颜色。',
    ui_correction: '尝试修正画面中的界面元素；游戏画面或带界面的素材可尝试开启。',
    auto_mask: '启用自动遮罩辅助处理。默认关闭，建议对比开启前后的效果。',
    hdr: '使用线性色彩进行增强处理；不会仅凭此选项将普通图片变成 HDR 文件。',
    motion_engine: '选择视频运动估计方式。推荐自动选择，按可用能力匹配。',
    motion: '利用帧间运动信息辅助视频增强，默认开启。',
    motion_vis: '显示光流调试画面。正常导出请保持关闭。',
    codec: '选择视频压缩编码。显卡编码较快；不可用时可选择 CPU 编码。',
    container: '选择输出文件格式，选项随编码方式变化。',
    bit_depth: '选择编码位深。10 bit 可减少色阶断层，8 bit 通常兼容性更好。',
    cq: '恒定画质参数，数值越低通常画质越高、文件越大；默认 19。',
    bitrate: '指定目标码率，单位 kbit/s。0 使用画质控制，非零值优先按码率编码。',
    audio: '选择保留、转换或移除音轨；推荐自动选择。',
    enc_preset: '显卡编码的速度与质量取舍。p1 偏快，p7 偏质量，默认 p5。',
    prores_profile: '选择 ProRes 规格，影响质量和文件大小；默认 HQ。',
    frames: '限制处理的视频帧数，用于快速试效果；0 表示处理全部。'
  };
  function describe(control, text) {
    control.title = text;
    control.setAttribute('aria-description', text);
    for (const label of control.labels || []) label.title = text;
    const row = control.closest('.advanced-range');
    if (row) row.title = text;
  }
  for (const control of document.querySelectorAll('#settings-form [name]')) {
    if (help[control.name]) describe(control, help[control.name]);
  }
  describe(document.querySelector('#intensity-number'), help.intensity);
  describe(document.querySelector('#quality'), '选择视频输出画质。CQ 越低通常质量越高、文件越大；自定义可设置 CQ 或码率。');
  const styles = {
    '默认': '使用默认画面风格，建议先用此项对比增强效果。',
    '自然': '选择自然风格；实际效果取决于素材，可与默认风格对比。',
    '电影': '选择电影风格；建议对比光影和色调是否符合预期。'
  };
  for (const button of document.querySelectorAll('[data-style]')) describe(button, styles[button.dataset.style]);
  for (const button of document.querySelectorAll('.restore,.inline-restore button')) {
    describe(button, '将全部高级强度恢复为 100%，并关闭高级勾选项，不改变输出分辨率和画面风格。');
  }
})();
