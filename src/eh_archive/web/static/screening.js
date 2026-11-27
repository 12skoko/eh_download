(() => {
  const root = document.querySelector("#screen-editor");
  if (!root || root.dataset.ready) return;
  root.dataset.ready = "1";
  const find = selector => root.querySelector(selector);
  const stored = JSON.parse(find("#screen-trees").textContent);
  const fields = {title:"档案标题", group_title:"归组标题", tags:"EH 标签", tag_text:"标签文本",
    category:"分类", uploader:"上传者", pages:"页数", rating:"列表评分（星）", posted_at:"发布时间（UTC）"};
  const namespaces = {language:"语言", artist:"作者", group:"社团", parody:"原作", character:"角色", "":"全部标签"};
  const languages = {chinese:"中文", japanese:"日文", english:"英文", translated:"已翻译", korean:"韩文",
    french:"法文", german:"德文", spanish:"西班牙文", russian:"俄文", portuguese:"葡萄牙文"};
  const categories = ["Manga","Doujinshi","Artist CG","Game CG","Western","Non-H","Image Set","Cosplay","Asian Porn","Misc"];
  const textOps = {contains:"包含", word:"按完整词匹配", equals:"完全等于", starts:"开头为", ends:"结尾为", regex:"正则"};
  const emptyOps = {empty:"为空", present:"有值"};
  const relations = {all:"满足全部条件（与）",any:"满足任一条件（或）"};
  const groups = {...relations,not:"不满足下方条件（非）"};
  const pathNames = {direct:"直接放行", algorithm:"算法候选"};
  const drafts = new WeakMap();
  const scopes = new WeakMap();
  let tab = "direct", editVersion = 0;
  const leaf = () => ({field:"title", op:"contains", values:[]});
  const group = () => ({op:"all", children:[leaf()]});

  function exclusionShell(rule) {
    return rule?.op === "not" && !rule.note;
  }
  function paired(rule) {
    return rule?.op === "all" && !rule.note && rule.children.length === 2 && exclusionShell(rule.children[1]);
  }
  function singleShell(rule) {
    return rule?.op === "all" && !rule.note && rule.children.length === 1 && !!rule.children[0].children;
  }
  function panel(rule, defaultOp) {
    if (rule && !rule.note && rule.op in relations) return {group:rule, implicit:false};
    // The header also serves single stored conditions, without adding a level to their JSON.
    return {group:{op:defaultOp, children:rule ? [rule] : []}, implicit:!!rule};
  }
  function openRule(saved) {
    const rule = saved?.notes ? saved.rule : saved;
    let include = rule, exclude = null;
    if (paired(rule)) {
      include = rule.children[0];
      exclude = rule.children[1].children[0];
    } else if (exclusionShell(rule)) {
      include = null;
      exclude = rule.children[0];
    } else if (singleShell(rule)) {
      include = rule.children[0];
    }
    return {include:panel(include, "all"), exclude:panel(exclude, "any"),
      keepIncludeWrapper:singleShell(rule), legacyNotes:saved?.notes};
  }
  const rules = {direct:openRule(stored.direct), algorithm:openRule(stored.algorithm)};

  function panelRule(side) {
    const {children} = side.group;
    if (!children.length) return null;
    return side.implicit && children.length === 1 ? children[0] : side.group;
  }
  function composeRule(rule) {
    const include = panelRule(rule.include), exclude = panelRule(rule.exclude);
    if (exclude) {
      const veto = {op:"not", children:[exclude]};
      return include ? {op:"all", children:[include, veto]} : veto;
    }
    // A one-child AND can preserve an include group whose shape itself looks like a shell.
    if (include && (rule.keepIncludeWrapper || paired(include) || singleShell(include) || exclusionShell(include))) {
      return {op:"all", children:[include]};
    }
    return include;
  }

  function ruleDocument(editor) {
    const rule = composeRule(editor);
    return editor.legacyNotes ? {rule, notes:editor.legacyNotes} : rule;
  }

  function ops(field) {
    if (field === "category" || field === "tags") return {exact:"匹配", none:"均不匹配", ...emptyOps};
    if (["pages","rating","posted_at"].includes(field)) {
      const date = field === "posted_at";
      const result = {eq:date ? "当日" : "等于",gte:date ? "自该日起" : "至少",lte:date ? "截至该日" : "至多",between:"区间（含两端）"};
      if (field === "posted_at") result.recent = "最近 N 天（含今天）";
      return {...result,...emptyOps};
    }
    return {...textOps,...emptyOps};
  }
  function element(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  function button(text, fn) {
    const el = element("button", text, "btn btn-sm");
    el.type = "button";
    el.onclick = fn;
    return el;
  }
  function select(options, active, fn, label) {
    const el = element("select", undefined, "select");
    el.setAttribute("aria-label", label);
    for (const [key, name] of Object.entries(options)) el.add(new Option(name, key));
    el.value = active;
    el.onchange = () => fn(el.value);
    return el;
  }
  function control(caption, input) {
    const label = element("label", undefined, "rule-control");
    label.append(element("span", caption), input);
    return label;
  }
  let dictionaryRequest, listId = 0;
  function tagDictionary() {
    if (!dictionaryRequest) {
      dictionaryRequest = fetch(location.pathname + "/tag-dictionary").then(async response => {
        if (!response.ok) throw new Error("翻译词库暂不可用");
        const groups = await response.json();
        return Object.entries(groups).map(([ns, group]) => ({
          ns, label: namespaces[ns] || group.label, translated: group.label,
          tags: Object.entries(group.tags).map(([name, translation]) => {
            const label = ns === "language" ? (languages[name] || translation) : translation;
            return {value: ns + ":" + name, name: name.toLowerCase(), label,
              translated: translation.toLowerCase()};
          })
        }));
      }).catch(error => {dictionaryRequest = null; throw error;});
    }
    return dictionaryRequest;
  }
  function findTags(groups, query, scope, selected) {
    const parts = query.toLowerCase().replace("：", ":").split(":");
    const prefix = parts.length > 1 ? parts.shift().trim() : "";
    const term = parts.join(":").trim();
    const ranked = [[], [], [], []];
    for (const {ns, label, translated, tags} of groups) {
      if (scope && scope !== ns) continue;
      const names = [ns, label, translated];
      if (prefix && !names.includes(prefix)) continue;
      const inNamespace = !term || names.some(name => name.startsWith(term));
      for (const tag of tags) {
        let rank = 3;
        if (term && (tag.name === term || tag.translated === term || tag.label === term)) rank = 0;
        else if (term && (tag.name.startsWith(term) || tag.translated.startsWith(term) || tag.label.startsWith(term))) rank = 1;
        else if (term && (tag.name.includes(term) || tag.translated.includes(term) || tag.label.includes(term))) rank = 2;
        else if (!inNamespace) continue;
        if (ranked[rank].length < 10 && !selected.has(tag.value)) ranked[rank].push({tag, label});
      }
    }
    return ranked.flat().slice(0, 10);
  }
  function tagMenu(editor, input, scope, values, choose) {
    const anchor = element("div", undefined, "rule-tag-entry");
    const popup = element("div", undefined, "rule-tag-menu");
    const list = element("div");
    const status = element("div", undefined, "rule-tag-status");
    list.id = "rule-tag-options-" + ++listId;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "EH 标签候选");
    status.setAttribute("role", "status");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", list.id);
    editor.replaceWith(anchor);
    anchor.append(editor, popup);
    popup.append(list, status);
    let active = -1, generation = 0, timer;
    function close() {
      clearTimeout(timer);
      timer = null;
      generation++;
      active = -1;
      list.replaceChildren();
      popup.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }
    function search() {
      close();
      const query = input.value.trim();
      if (!query) return;
      const current = generation;
      timer = setTimeout(async () => {
        timer = null;
        if (!input.isConnected) return;
        popup.hidden = false;
        status.textContent = "正在查找标签…";
        input.setAttribute("aria-expanded", "true");
        try {
          const groups = await tagDictionary();
          if (current !== generation || !input.isConnected) return;
          const matches = findTags(groups, query, scope, new Set(values));
          status.textContent = matches.length ? "" : "词库中没有匹配的标签";
          for (const {tag, label} of matches) {
            const option = element("div", undefined, "rule-tag-option");
            option.id = list.id + "-" + list.children.length;
            option.setAttribute("role", "option");
            option.setAttribute("aria-selected", "false");
            option.append(element("span", label + " · " + tag.label), element("code", tag.value));
            option.onmousedown = ev => ev.preventDefault();
            option.onclick = () => choose(tag.value);
            list.append(option);
          }
        } catch {
          if (current === generation) status.textContent = "翻译词库暂不可用，可手动输入完整标签";
        }
      }, 100);
    }
    function keydown(ev) {
      if (ev.key === "Escape" && (!popup.hidden || timer !== null)) {close(); return true;}
      const options = list.children;
      if (!options.length) return false;
      if (ev.key === "Enter" && active >= 0) {options[active].click(); return true;}
      if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return false;
      options[active]?.setAttribute("aria-selected", "false");
      active = ev.key === "ArrowDown" ? (active + 1) % options.length
        : (active <= 0 ? options.length - 1 : active - 1);
      const option = options[active];
      option.setAttribute("aria-selected", "true");
      input.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({block:"nearest"});
      return true;
    }
    close();
    return {search, close, keydown};
  }
  const ruleDetails = find("[data-rule-details]");
  function refreshRuleJson() {
    if (ruleDetails.open) find("[data-rule-json]").textContent = JSON.stringify(ruleDocument(rules[tab]), null, 2);
  }
  ruleDetails.addEventListener("toggle", refreshRuleJson);
  let treeFrame = 0;
  function changed() {
    editVersion++;
    find("[data-results]").replaceChildren();
    find("[data-status]").textContent = "草稿已修改";
    refreshRuleJson();
    // 侧栏里的输入只刷新左侧大纲，不重建侧栏，避免输入框失焦。
    if (!treeFrame) treeFrame = requestAnimationFrame(() => {treeFrame = 0; renderTree();});
  }
  function updateNode(node, obj) {
    for (const key of Object.keys(node)) {
      if (key !== "note") delete node[key];
    }
    Object.assign(node, obj);
    changed();
    render();
  }
  function blank(field, op) {
    const n = {field,op};
    if (op in emptyOps) return n;
    if (["pages","rating","posted_at"].includes(field)) {
      n.value = op === "between" ? [null,null] : null;
    } else n.values = [];
    return n;
  }
  async function copyValues(box, trigger, values, kind) {
    const text = values.join("\n");
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        window.archiveUI?.toast("已复制" + kind);
        return;
      } catch {
        // Clipboard permission can be denied even on HTTPS.
      }
    }
    if (!box.isConnected) return;
    const list = element("textarea", undefined, "textarea mono");
    list.readOnly = true;
    list.setAttribute("aria-label", "可复制的" + kind);
    list.value = text;
    box.append(list);
    list.addEventListener("blur", () => list.remove(), {once:true});
    list.focus();
    list.select();
    if (document.execCommand("copy")) {
      trigger.focus();
      window.archiveUI?.toast("已复制" + kind);
    } else {
      window.archiveUI?.toast("已选中" + kind + "，请使用系统复制操作");
    }
  }
  function editValues(box, controls, n) {
    const tags = n.field === "tags";
    const cats = n.field === "category";
    let ns = "";
    if (tags) {
      ns = scopes.get(n) ?? "";
      controls.append(control("标签范围", select(namespaces, ns, v => {scopes.set(n,v); render();}, "添加标签的范围")));
    }
    if (!cats && n.op !== "none") {
      const choices = {any:"命中任一值", all:"命中全部值"};
      controls.append(control("多个值", select(choices, n.match || "any", v => {n.match=v; changed();}, "多值关系")));
    }
    const meta = element("div", undefined, "rule-value-meta");
    if (n.op in textOps) {
      const label = element("label", undefined, "rule-case"), cb = element("input");
      cb.type = "checkbox"; cb.checked = !!n.case;
      cb.onchange = () => {n.case=cb.checked; changed();};
      label.append(cb, "区分大小写"); meta.append(label);
    }
    const kind = tags ? "标签" : cats ? "分类" : n.op === "regex" ? "表达式" : "关键词";
    const count = element("span", undefined, "rule-value-count");
    const copy = button("复制" + kind, () => copyValues(box, copy, n.values, kind));
    copy.classList.add("btn-ghost");
    meta.append(count, copy);
    const updateCount = () => {
      count.textContent = n.values.length + " 个" + kind;
      copy.disabled = n.values.length === 0;
    };
    const entry = element("div", undefined, "rule-entry");
    const valueField = element("div", undefined, "rule-control rule-value-field");
    valueField.append(element("span", "值"), entry);
    box.append(valueField);
    controls.append(meta);
    if (cats || (tags && ns === "language")) {
      const choices = cats ? Object.fromEntries(categories.map(v => [v,v])) : languages;
      const menu = select({"":"选择并添加", ...choices}, "", v => {
        if (v) replaceValues([v]);
        menu.value = "";
      }, "添加常用值");
      entry.append(menu);
    }
    const editor = element("div", undefined, "chip-box rule-values");
    const input = element("textarea", undefined, "chip-input");
    const separator = n.op === "regex" ? /\r?\n/ : /[;；\n]/;
    let editing = null;
    input.rows = 1;
    input.placeholder = n.op === "regex" ? "回车添加，多个正则用换行分隔" : "用分号或回车添加";
    if (tags) input.placeholder = "输入中文或英文查找标签";
    input.setAttribute("aria-label", "输入或编辑" + kind);
    input.spellcheck = false;
    editor.append(input);
    entry.append(editor);
    const suggestions = tags ? tagMenu(editor, input, ns, n.values, tag => {
      finish(false, tag);
      input.focus();
    }) : null;

    function makeChip(value) {
      const chip = element("span", undefined, "rule-chip");
      const name = value;
      const edit = element("button", name, "rule-chip-name");
      edit.type = "button";
      edit.setAttribute("aria-label", "编辑 " + name);
      edit.onclick = () => {
        chip.before(input);
        editing = {chip, value};
        chip.hidden = true;
        input.value = value;
        input.focus();
        input.select();
      };
      const remove = button("×", () => {replaceValues([], chip); input.focus();});
      remove.setAttribute("aria-label", "移除 " + name);
      chip.append(edit, remove);
      return chip;
    }
    function replaceValues(values, chip = null) {
      const index = chip ? Array.from(editor.children).indexOf(chip) : n.values.length;
      if (chip) n.values.splice(index, 1);
      const seen = new Set(n.values);
      const added = [];
      for (let value of values) {
        value = value.trim();
        if (!value) continue;
        if (tags && ns && !value.includes(":")) {
          if (ns === "language") value = Object.keys(languages).find(k => languages[k] === value) || value;
          value = ns + ":" + value;
        }
        if (seen.has(value)) continue;
        seen.add(value);
        added.push(value);
        (chip || input).before(makeChip(value));
      }
      n.values.splice(index, 0, ...added);
      chip?.remove();
      updateCount();
      changed();
    }
    function finish(cancel = false, selectedTag = null) {
      suggestions?.close();
      const text = selectedTag ?? input.value;
      const original = editing;
      editing = null;
      input.value = "";
      if (original) {
        original.chip.hidden = false;
        editor.append(input);
      }
      if (cancel || !text.trim() || text === original?.value) return;
      replaceValues(selectedTag ? [selectedTag] : text.split(separator), original?.chip);
    }
    function accept() { finish(); input.focus(); }
    input.onblur = () => finish();
    input.oninput = ev => {
      if (!ev.isComposing && separator.test(input.value)) accept();
      else {
        if (!ev.isComposing) suggestions?.search();
        changed();
      }
    };
    input.addEventListener("compositionend", () => {
      if (separator.test(input.value)) accept();
      else suggestions?.search();
    });
    input.addEventListener("compositionstart", () => suggestions?.close());
    input.onkeydown = ev => {
      if (ev.isComposing || ev.keyCode === 229) return;
      if (suggestions?.keydown(ev)) {
        ev.preventDefault();
      } else if (ev.key === "Enter") {
        ev.preventDefault(); accept();
      } else if (ev.key === "Escape" && editing) {
        ev.preventDefault(); finish(true); input.focus();
      }
    };
    editor.onclick = ev => {if (ev.target === editor) input.focus();};
    for (const value of n.values) input.before(makeChip(value));
    updateCount();
  }
  function editRange(box, n) {
    const range = n.op === "between";
    for (const [i,v] of (range ? n.value : [n.value]).entries()) {
      const input = element("input", undefined, "input");
      input.type = n.field === "posted_at" && n.op !== "recent" ? "date" : "number";
      input.setAttribute("aria-label", range ? (i ? "上限" : "下限") : "条件值");
      if (input.type === "number") {
        input.min = n.op === "recent" ? "1" : "0";
        if (n.field === "rating") {input.max="5"; input.step="0.5";}
      }
      input.value = v ?? "";
      input.oninput = () => {
        const value = input.type === "date" ? input.value : (input.value ? input.valueAsNumber : null);
        if (range) n.value[i] = value; else n.value = value;
        changed();
      };
      box.append(control(input.getAttribute("aria-label"), input));
    }
  }
  function height(n) {
    return n.children?.length ? 1 + Math.max(...n.children.map(height)) : 0;
  }
  // ---------- 大纲 + 侧栏 ----------
  // 左侧大纲只读，点行选中，在右侧侧栏编辑；组前的箭头只负责折叠。
  const relationNames = {all:"以下全部满足", any:"以下任一满足", nall:"不满足以下全部", nany:"不满足以下任一", not:"不满足以下条件"};
  const treeBox = find("[data-tree]"), sidePanel = find("[data-side]");
  const collapsed = new WeakSet();
  let selected = null, contexts = new Map();

  function groupView(raw) {
    const inner = raw.children[0];
    // 不带备注的 not 包着一个与 / 或组：显示为“不满足以下全部 / 任一”，不多占一层。
    if (raw.op === "not" && !raw.note && inner?.children && inner.op in relations) {
      return {wrapper:raw, inner, relation:"n" + inner.op};
    }
    return {wrapper:null, inner:raw, relation:raw.op};
  }
  function chevron() {
    const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"), path = document.createElementNS(ns, "path");
    svg.setAttribute("viewBox", "0 0 12 12");
    svg.setAttribute("aria-hidden", "true");
    path.setAttribute("d", "M3 4.5 6 7.5 9 4.5");
    svg.append(path);
    return svg;
  }
  function summarize(group) {
    const groups = group.children.filter(child => child.children && !(exclusionShell(child) && !child.children[0].children)).length;
    const leaves = group.children.length - groups;
    return [leaves && leaves + " 个条件", groups && groups + " 个子组"].filter(Boolean).join("、");
  }
  function valueChips(n) {
    const wrap = element("span", undefined, "rule-view-values");
    const line = element("span", undefined, "rule-view-chips");
    for (const value of n.values) line.append(element("span", value, "rule-view-chip" + (n.field === "tags" ? " mono" : "")));
    const more = element("span", undefined, "rule-view-more");
    more.hidden = true;
    line.append(more);
    wrap.append(line);
    return wrap;
  }
  function leafSentence(n, negated) {
    const sentence = element("span", undefined, "rule-sentence");
    if (negated) sentence.append(element("span", "取反", "rule-neg-tag"));
    if (n.op === "always") {
      sentence.append(element("span", "匹配全部档案", "rule-field"));
      return sentence;
    }
    let op = ops(n.field)[n.op] || n.op;
    if (n.op === "recent") op = "最近 " + (n.value ?? "?") + " 天（含今天）";
    else if (n.values && n.values.length > 1 && n.op !== "none") op += n.match === "all" ? "全部" : "任一";
    sentence.append(element("span", fields[n.field] || n.field, "rule-field"), element("span", op, "rule-op"));
    if (n.values) sentence.append(n.values.length ? valueChips(n) : element("span", "（未填写）", "rule-op"));
    else if ("value" in n && n.op !== "recent") {
      const value = Array.isArray(n.value) ? n.value.map(v => v ?? "?").join(" ～ ") : (n.value ?? "?");
      sentence.append(element("span", String(value), "rule-number"));
    }
    if (n.case) sentence.append(element("span", "区分大小写", "rule-op"));
    return sentence;
  }
  // 值只显示一行：放不下的隐藏，在最后一个可见值后面写“等 N 个”。
  function fitValues() {
    for (const line of treeBox.querySelectorAll(".rule-view-chips")) {
      const chips = [...line.querySelectorAll(".rule-view-chip")], more = line.querySelector(".rule-view-more");
      chips.forEach(chip => {chip.hidden = false;});
      more.hidden = true;
      if (!line.offsetParent || !chips.length) continue;
      const top = chips[0].offsetTop, bottom = top + chips[0].offsetHeight;
      if (!chips.some(chip => chip.offsetTop >= bottom)) continue;
      more.hidden = false;
      more.textContent = "等 " + chips.length + " 个";
      for (let i = chips.length - 1; i >= 1; i--) {
        chips[i].hidden = true;
        if (more.offsetTop < bottom && !chips.slice(0, i).some(chip => chip.offsetTop >= bottom)) break;
      }
    }
  }
  new ResizeObserver(() => fitValues()).observe(treeBox);

  function treeRow(ctx, cells) {
    const row = element("div", undefined, "rule-row" + (ctx.kind === "group" ? " is-group" : ""));
    row.style.setProperty("--indent", ctx.level * 22 + "px");
    row.tabIndex = 0;
    row.setAttribute("role", "treeitem");
    row.setAttribute("aria-level", String(ctx.level + 1));
    row.setAttribute("aria-selected", String(selected === ctx.inner));
    if (selected === ctx.inner) row.classList.add("is-selected");
    const toggleCell = element("span", undefined, "rule-row-toggle");
    if (ctx.kind === "group") {
      const open = !collapsed.has(ctx.inner);
      row.setAttribute("aria-expanded", String(open));
      const toggle = element("button", undefined, "rule-toggle");
      toggle.type = "button";
      toggle.tabIndex = -1;
      toggle.setAttribute("aria-label", open ? "折叠" : "展开");
      toggle.append(chevron());
      toggle.onclick = ev => {
        ev.stopPropagation();
        if (open) collapsed.add(ctx.inner); else collapsed.delete(ctx.inner);
        renderTree();
        treeBox.querySelector(".rule-row.is-focus-target")?.focus();
      };
      toggleCell.append(toggle);
    }
    row.append(element("span", undefined, "rule-guides"), element("span", ctx.join, "rule-row-join"), toggleCell, cells);
    row.onclick = () => choose(ctx.inner);
    row.onkeydown = ev => {
      if (ev.target !== row) return;
      if (ev.key === "Enter" || ev.key === " ") {ev.preventDefault(); choose(ctx.inner);}
      else if (ctx.kind === "group" && (ev.key === "ArrowLeft" || ev.key === "ArrowRight")) {
        const close = ev.key === "ArrowLeft";
        if (close === collapsed.has(ctx.inner)) return;
        ev.preventDefault();
        row.querySelector(".rule-toggle").click();
      }
    };
    row.addEventListener("focus", () => {
      treeBox.querySelectorAll(".is-focus-target").forEach(el => el.classList.remove("is-focus-target"));
      row.classList.add("is-focus-target");
    });
    return row;
  }
  function groupRow(ctx) {
    const cells = element("span", undefined, "rule-row-content");
    if (!ctx.root && ctx.inner.note) cells.append(element("span", ctx.inner.note, "rule-group-name"));
    const negated = ctx.relation === "nall" || ctx.relation === "nany" || ctx.relation === "not";
    cells.append(element("span", relationNames[ctx.relation] || ctx.relation, "rule-relation" + (negated ? " is-negated" : "")));
    if (collapsed.has(ctx.inner)) cells.append(element("span", "（" + summarize(ctx.inner) + "）", "rule-op"));
    return treeRow(ctx, cells);
  }
  function leafRow(ctx) {
    const cells = element("span", undefined, "rule-row-content rule-leaf-content");
    cells.append(leafSentence(ctx.inner, !!ctx.wrapper));
    if (ctx.inner.note) cells.append(element("span", ctx.inner.note, "rule-leaf-note"));
    return treeRow(ctx, cells);
  }
  function walk(tree, ctx, hidden) {
    const raw = ctx.raw;
    if (raw.children && !(exclusionShell(raw) && !raw.children[0]?.children)) {
      Object.assign(ctx, ctx.root ? {wrapper:null, inner:raw, relation:raw.op} : groupView(raw), {kind:"group"});
      ctx.innerDepth = ctx.depth + (ctx.wrapper ? 1 : 0);
      contexts.set(ctx.inner, ctx);
      if (!hidden) tree.append(groupRow(ctx));
      const list = ctx.inner.children;
      list.forEach((child, i) => walk(tree, {
        raw:child, list, index:i, parent:ctx, depth:ctx.innerDepth + 1, level:ctx.level + 1,
        join:i ? (ctx.inner.op === "all" ? "且" : "或") : "", sideName:ctx.sideName,
        replace:value => {
          if (value) list[i] = value;
          else {
            list.splice(i, 1);
            if (!list.length && !ctx.root) ctx.replace(null);
          }
        }
      }, hidden || collapsed.has(ctx.inner)));
    } else {
      const negated = exclusionShell(raw);
      Object.assign(ctx, {kind:"leaf", wrapper:negated ? raw : null, inner:negated ? raw.children[0] : raw});
      contexts.set(ctx.inner, ctx);
      if (!hidden) tree.append(leafRow(ctx));
    }
  }
  function renderTree() {
    const refocus = treeBox.contains(document.activeElement);
    const rule = rules[tab], combined = composeRule(rule), parts = [];
    contexts = new Map();
    for (const sideName of ["include", "exclude"]) {
      const side = rule[sideName], branch = panelRule(side);
      let depth = sideName === "exclude" ? (panelRule(rule.include) ? 2 : 1)
        : (combined && branch !== combined ? 1 : 0);
      if (branch && branch !== side.group) depth--;
      const title = sideName === "include" ? "包含条件" : "排除条件";
      if (sideName === "exclude") parts.push(element("div", "并且", "rule-panel-join"));
      const tree = element("div", undefined, "rule-tree");
      tree.setAttribute("role", "tree");
      tree.setAttribute("aria-label", title);
      tree.dataset.rulePanel = sideName;
      walk(tree, {raw:side.group, root:true, panelRoot:side, sideName, depth, level:0, join:""}, false);
      if (!side.group.children.length) {
        const hasOtherSide = !!panelRule(rule[sideName === "include" ? "exclude" : "include"]);
        const hint = sideName === "exclude" ? "未设置排除条件"
          : hasOtherSide ? "包含所有未命中排除条件的档案" : "两框都留空时，此路径不匹配档案";
        tree.append(element("div", hint, "rule-empty"));
      }
      parts.push(element("h2", title, "rule-panel-title"), tree);
    }
    treeBox.replaceChildren(...parts);
    fitValues();
    if (refocus) treeBox.querySelector(".rule-row.is-selected")?.focus();
  }

  function choose(key) {
    selected = key;
    renderTree();
    renderSide();
  }
  function noteField(n, kind) {
    const input = element("input", undefined, "input");
    input.value = n.note || "";
    input.placeholder = kind === "group" ? "可选，填写后作为组名显示" : "可选，显示在条件下方";
    input.oninput = () => {
      if (input.value.trim()) n.note = input.value; else delete n.note;
      changed();
    };
    return control("备注", input);
  }
  function groupFields(ctx, box) {
    const options = ctx.root ? relations : Object.fromEntries(Object.entries(relationNames)
      .filter(([key]) => key !== "not" || ctx.relation === "not"));
    box.append(control("组内关系", select(options, ctx.relation, value => {
      if (value === ctx.relation) return;
      if (ctx.root) {
        ctx.inner.op = value;
        ctx.panelRoot.implicit = false;
      } else if (value in relations) {
        ctx.inner.op = value;
        if (ctx.wrapper) ctx.replace(ctx.inner);
      } else {
        const base = value.slice(1);
        if (!ctx.wrapper && ctx.depth + height(ctx.inner) >= 8) {
          render();
          find("[data-status]").textContent = "已达到 8 层嵌套限制";
          return;
        }
        ctx.inner.op = base;
        if (!ctx.wrapper) ctx.replace({op:"not", children:[ctx.inner]});
      }
      changed(); render();
    }, "组内关系")));
  }
  function leafFields(ctx, box) {
    const n = ctx.inner, excluded = !!ctx.wrapper;
    const modes = element("div", undefined, "seg");
    modes.setAttribute("role", "group");
    modes.setAttribute("aria-label", "条件模式");
    for (const [label, negate] of [["匹配", false], ["取反", true]]) {
      const option = element("button", label);
      option.type = "button";
      option.setAttribute("aria-pressed", String(excluded === negate));
      option.disabled = negate && !excluded && ctx.depth >= 8;
      if (option.disabled) option.title = "已达到 8 层嵌套限制";
      option.onclick = () => {
        if (excluded === negate) return;
        ctx.replace(negate ? {op:"not", children:[n]} : n);
        changed(); render();
      };
      modes.append(option);
    }
    const mode = element("div", undefined, "rule-control");
    mode.append(element("span", "模式"), modes);
    box.append(mode);
    if (n.op === "always") {
      box.append(element("p", "匹配全部档案", "rule-empty"));
      return;
    }
    box.append(control("筛选字段", select(fields, n.field, f => {
      const cache = drafts.get(n) || {};
      cache[n.field] = structuredClone(n);
      delete cache[n.field].note;
      drafts.set(n, cache);
      const op = n.op in ops(f) ? n.op : Object.keys(ops(f))[0];
      const next = cache[f] || blank(f, op);
      if (!cache[f] && next.values && n.values && f in fields &&
          n.op in textOps && op in textOps) next.values = [...n.values];
      updateNode(n, next);
    }, "筛选字段")), control("匹配方式", select(ops(n.field), n.op, op => {
      const next = blank(n.field, op);
      if (next.values && n.values) next.values = n.values;
      if (next.values && n.case) next.case = true;
      if (next.values && n.match && op !== "none") next.match = n.match;
      updateNode(n, next);
    }, "匹配方式")));
    if (n.values) editValues(box, box, n);
    if ("value" in n) editRange(box, n);
  }
  function sideActions(ctx) {
    const bar = element("div", undefined, "rule-side-actions");
    const add = (label, rule) => button(label, () => {
      ctx.inner.children.push(rule);
      if (ctx.root) ctx.panelRoot.implicit = false;
      selected = exclusionShell(rule) ? rule.children[0] : rule;
      changed(); render();
    });
    if (ctx.kind === "group" && ctx.inner.op !== "not" && ctx.innerDepth < 8) {
      bar.append(add("＋ 条件", leaf()));
      if (ctx.innerDepth < 7) bar.append(add("＋ 子组", group()));
    }
    if (ctx.root && ctx.sideName === "include" && !ctx.inner.children.length) {
      bar.append(button("匹配全部", () => {
        ctx.panelRoot.implicit = true;
        ctx.inner.children.push({op:"always"});
        changed(); render();
      }));
    }
    if (!ctx.root) {
      const move = (label, delta) => {
        const target = ctx.index + delta, el = button(label, () => {
          [ctx.list[ctx.index], ctx.list[target]] = [ctx.list[target], ctx.list[ctx.index]];
          changed(); render();
        });
        el.classList.add("btn-ghost");
        el.disabled = target < 0 || target >= ctx.list.length;
        return el;
      };
      bar.append(move("上移", -1), move("下移", 1));
      if (ctx.kind === "leaf") {
        const copy = button("复制条件", () => {
          const clone = structuredClone(ctx.raw);
          ctx.list.splice(ctx.index + 1, 0, clone);
          selected = exclusionShell(clone) ? clone.children[0] : clone;
          changed(); render();
        });
        copy.classList.add("btn-ghost");
        bar.append(copy);
      }
      const remove = button(ctx.kind === "group" ? "删除组" : "删除条件", () => {
        selected = ctx.parent.inner;
        ctx.replace(null);
        changed(); render();
      });
      remove.classList.add("btn-ghost", "rule-remove");
      bar.append(remove);
    }
    return bar;
  }
  function renderSide() {
    const ctx = selected && contexts.get(selected);
    sidePanel.replaceChildren();
    if (!ctx) {
      selected = null;
      sidePanel.append(element("h3", "编辑"), element("p", "在左侧点选一个条件或组。", "rule-empty"));
      return;
    }
    const title = ctx.kind === "leaf" ? "编辑条件"
      : ctx.root ? (ctx.sideName === "include" ? "编辑包含条件" : "编辑排除条件") : "编辑组";
    const box = element("div", undefined, "rule-side-fields");
    sidePanel.append(element("h3", title), box);
    if (!ctx.root) box.append(noteField(ctx.inner, ctx.kind));
    if (ctx.kind === "group") groupFields(ctx, box); else leafFields(ctx, box);
    sidePanel.append(sideActions(ctx));
  }
  function render() {
    for (const el of root.querySelectorAll("[data-tab]")) el.setAttribute("aria-pressed", String(el.dataset.tab === tab));
    renderTree();
    renderSide();
    refreshRuleJson();
  }
  // Committing on blur can rewrap chips. Keep the button under the pointer until click.
  root.addEventListener("mousedown", ev => {
    if (ev.button === 0 && ev.target.closest("button") && document.activeElement.matches(".rule-values textarea")) {
      ev.preventDefault();
    }
  });
  root.addEventListener("click", ev => {
    const target = ev.target.closest("button");
    if (target && document.activeElement.matches(".rule-values textarea")) target.focus({preventScroll:true});
  }, true);
  root.querySelectorAll("[data-tab]").forEach(el => el.onclick = () => {tab=el.dataset.tab; selected=null; render();});

  function explainValues(rule, step) {
    const hit = rule.op === "none" ? !step.matched : step.matched;
    const allValues = rule.match === "all";
    if (hit !== allValues) {
      return element("p", (hit ? "命中值" : "首个未命中值") + "「" + rule.values[step.last] + "」");
    }
    const label = hit ? "全部命中" : "均未命中";
    const words = rule.values.map(value => "「" + value + "」").join("、");
    if (rule.values.length <= 3) return element("p", label + " " + words);
    const more = element("details");
    more.append(element("summary", rule.values.length + " 个值" + label), element("p", words));
    return more;
  }

  function explainNode(rule, step, actual) {
    const item = element("div", undefined, "preview-check");
    const verdict = step.matched ? "命中" : "未命中";
    const heading = element("strong");
    item.append(heading);
    if (rule.note) item.append(element("p", rule.note, "preview-note"));
    if (rule.children) {
      const relation = rule.op === "not" ? "取反后" : relations[rule.op];
      heading.textContent = relation + " · " + verdict;
      step.children.forEach((child, i) => item.append(explainNode(rule.children[i], child, actual)));
      const skipped = rule.children.length - step.children.length;
      if (skipped) item.append(element("p", "本组结果已确定，余下 " + skipped + " 项未检查", "muted"));
    } else if (rule.op === "always") {
      heading.textContent = "匹配全部档案 · 命中";
    } else {
      const op = ops(rule.field)[rule.op];
      const target = Array.isArray(rule.value) ? rule.value.join(" ～ ") : rule.value;
      const condition = target == null ? op : op + " " + target;
      heading.textContent = fields[rule.field] + " · " + condition + " · " + verdict;
      if (rule.values) item.append(explainValues(rule, step));
      const value = actual[rule.field];
      const source = element("p", undefined, "preview-source");
      source.append(element("span", "档案原值"));
      if (step.matched && step.span && step.span[0] < step.span[1]) {
        const chars = Array.from(value), [start, end] = step.span;
        source.append(chars.slice(0,start).join(""), element("mark", chars.slice(start,end).join("")), chars.slice(end).join(""));
      } else {
        source.append(value === "" || value == null ? "（空）" : String(value));
      }
      item.append(source);
      if (rule.case) item.append(element("p", "区分大小写", "muted"));
    }
    return item;
  }

  function explainRoute(name, saved, step, actual) {
    const rule = saved?.notes ? saved.rule : saved;
    const box = element("section", undefined, "preview-route");
    const passed = name === "direct" ? "已通过" : "进入候选";
    const result = !step ? "已直接放行，跳过本路径" : !rule ? "未设置规则" : step.matched ? passed : "未通过";
    box.append(element("h3", pathNames[name] + " · " + result));
    if (!rule || !step) return box;
    function side(key, node, check) {
      const label = key === "include" ? "包含条件" : "排除条件";
      const state = !node ? "未设置" : !check ? "未检查" : check.matched ? "命中" : "未命中";
      box.append(element("h4", label + " · " + state));
      if (check) box.append(explainNode(node, check, actual));
    }
    if (paired(rule)) {
      side("include", rule.children[0], step?.children[0]);
      side("exclude", rule.children[1].children[0], step?.children[1]?.children[0]);
    } else if (exclusionShell(rule)) {
      side("include", null);
      side("exclude", rule.children[0], step?.children[0]);
    } else {
      const wrapped = singleShell(rule);
      side("include", wrapped ? rule.children[0] : rule, wrapped ? step?.children[0] : step);
      side("exclude", null);
    }
    return box;
  }

  function archiveResult(sample, draft) {
    const row = element("details", undefined, "preview-archive");
    const summary = element("summary");
    summary.append(element("code", sample.id), element("span", sample.title), element("span", "判定依据", "preview-why"));
    row.append(summary);
    row.addEventListener("toggle", () => {
      if (!row.open || row.childElementCount > 1) return;
      const {routes, fields:actual} = sample.trace;
      row.append(explainRoute("direct", draft.direct, routes[0], actual),
        explainRoute("algorithm", draft.algorithm, routes[1], actual));
    });
    return row;
  }

  function previewGroup(label, rows, draft) {
    const section = element("details", undefined, "preview-group"), list = element("div");
    section.append(element("summary", label + " · " + rows.length + " 条"), list);
    const pageSize = 10;
    let start = 0;
    const show = () => list.replaceChildren(...rows.slice(start, start + pageSize).map(row => archiveResult(row, draft)));
    if (rows.length > pageSize) {
      const nav = element("div", undefined, "preview-pages"), range = element("span");
      const prev = button("上一页", () => move(-pageSize)), next = button("下一页", () => move(pageSize));
      function move(delta) {
        start += delta;
        show();
        range.textContent = (start + 1) + "–" + Math.min(start + pageSize, rows.length) + " / " + rows.length;
        prev.disabled = start === 0;
        next.disabled = start + pageSize >= rows.length;
      }
      nav.append(prev, range, next);
      section.append(nav);
      move(0);
    } else show();
    return section;
  }

  async function send(action) {
    const params = new URLSearchParams({
      direct:JSON.stringify(ruleDocument(rules.direct)), algorithm:JSON.stringify(ruleDocument(rules.algorithm)),
      csrf_token:document.querySelector('meta[name="csrf-token"]').content, revision:root.dataset.revision,
      scope:find("[data-scope]").value, limit:find("[data-limit]").value
    });
    const version = editVersion, status = find("[data-status]");
    const controls = [find("[data-save]"),find("[data-preview]")];
    controls.forEach(el => el.disabled=true);
    try {
      const response = await fetch(location.pathname + "/" + action, {method:"POST",body:params});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "请求失败");
      if (action === "save") {
        root.dataset.revision = data.revision;
        if (version === editVersion) {
          location.assign("/settings?section=crawl&saved=next_worker");
        } else {
          status.textContent = "已保存提交时的规则，当前仍有未保存修改";
        }
      } else if (version === editVersion) {
        const result = find("[data-results]");
        result.replaceChildren();
        const outcomes = {queue:pathNames.direct,compare_group:pathNames.algorithm,filter_out:"滤除"};
        const draft = {direct:JSON.parse(params.get("direct")), algorithm:JSON.parse(params.get("algorithm"))};
        for (const [key,label] of Object.entries(outcomes)) {
          const group = previewGroup(label, data.samples[key], draft);
          group.open = data.counts[key] > 0 && !result.querySelector("details[open]");
          result.append(group);
        }
        status.textContent = "已预览 " + Object.values(data.counts).reduce((a,b)=>a+b,0) + " 条档案";
      }
    } catch (error) {status.textContent = error.message;}
    finally {controls.forEach(el => el.disabled=false);}
  }
  find("[data-scope]").onchange = find("[data-limit]").oninput = () => {
    editVersion++; find("[data-results]").replaceChildren(); find("[data-status]").textContent = "";
  };
  find("[data-save]").onclick = () => send("save");
  find("[data-preview]").onclick = () => send("preview");
  render();
})();
