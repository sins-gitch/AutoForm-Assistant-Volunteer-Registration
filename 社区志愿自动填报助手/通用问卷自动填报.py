# -*- coding: utf-8 -*-
"""
通用问卷填报助手：在窗口里粘贴任意 HTTP(S) 网址，点击按钮打开网页并填写。
默认需要人工核对才提交；提交后尝试保存进群二维码及结果页截图。
仅操作自己的报名资料；不绕过验证码、登录或网站限制。
"""
from __future__ import annotations

import json
import hashlib
import queue
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
PICTURE_DIR = BASE / "进群图片"
FIELD_DIR = BASE / "识别到的题目"
BROWSER_DIR = BASE / "浏览器资料"

FORMS = {
    "SfAh5f": "20261124宿舍走访",
    "oqNJvM": "1120学生社区劳动教育",
    "yw91OZ": "社区学堂志愿宣讲活动",
    "WFHVOJ": "1113学生社区劳动教育",
    "zEZCef": "1106学生社区劳动教育",
}
PROFILE_FIELDS = [
    "姓名", "学号", "学院", "专业", "班级", "年级",
    "性别", "政治面貌", "是否为楼栋长、楼层长", "手机号", "微信号", "QQ号", "校区",
    "宿舍楼", "宿舍号",
]
ALIASES = {
    "姓名": ["姓名", "学生姓名", "真实姓名", "你的名字", "您的姓名"],
    "学号": ["学号", "学生学号"],
    "学院": ["学院", "所在学院", "所属学院", "院系", "所属院系"],
    "专业": ["专业", "所学专业", "专业名称"],
    "班级": ["班级", "行政班", "所在班级"],
    "年级": ["年级", "入学年级"],
    "性别": ["性别", "学生性别", "您的性别"],
    "政治面貌": ["政治面貌", "政治身份", "党团身份", "政治情况", "政治属性"],
    "是否为楼栋长、楼层长": [
        "是否为楼栋长楼层长", "是否为楼道长楼层长",
        "是否楼栋长", "是否楼道长", "是否楼层长",
        "楼栋长", "楼道长", "楼层长",
    ],
    "手机号": ["手机号码", "手机号", "联系电话", "联系手机", "联系方式", "手机"],
    "微信号": ["微信号", "微信号码", "微信"],
    "QQ号": ["QQ号码", "QQ号", "QQ"],
    "校区": ["校区", "所在校区"],
    "宿舍楼": ["宿舍楼栋", "宿舍楼", "宿舍楼号", "寝室楼", "楼栋"],
    "宿舍号": ["宿舍房间号", "宿舍号", "寝室号", "房间号"],
}

DEFAULT_CONFIG = {
    "个人资料": {name: "" for name in PROFILE_FIELDS},
    "自动提交": False,
    "自动确认最后进群选项": True,
    "每张问卷额外答案": {k: {} for k in FORMS},
}

# 读取表单 DOM 中的标题、选项，适配金数据常见的 input、textarea、select、
# radio、checkbox；自定义复杂控件可能需要手动填写。
FIELD_META_JS = r"""(e) => {
  const norm = v => (typeof v === 'string' ? v : (v?.innerText || v?.textContent || ''))
    .replace(/\s+/g, ' ').replace(/^\s*[\*＊]\s*/, '').trim();
  const tag = e.tagName.toLowerCase();
  const type = (e.getAttribute('type') ||
    (tag === 'input' ? 'text' : tag)).toLowerCase();
  const inputs = 'input,textarea,select,[role="radio"],[role="checkbox"],[role="textbox"]';
  const seen = new Set();
  const candidates = [];
  const add = value => {
    const t = norm(value).slice(0, 110);
    if (!t || t.length > 95 || /^[*＊:：\s]+$/.test(t) || seen.has(t)) return;
    seen.add(t); candidates.push(t);
  };
  const short = value => norm(value).slice(0, 65);
  const labelList = Array.from(e.labels || [])
    .map(l => short(l)).filter(Boolean);
  const nativeChoice = type === 'radio' || type === 'checkbox';
  const roleChoice = ['radio', 'checkbox'].includes(e.getAttribute('role') || '');
  const isChoice = nativeChoice || roleChoice;

  // 选项文字与题目标题不同：例如“性别”是题目，“女”是选项。
  // React/金数据的自定义单选框常把值写成 female、ccp 等机器码，
  // 而实际显示的文字“女”“中共党员（含预备党员）”位于兄弟节点。
  // 逐层寻找“只包含当前一个选择控件”的最小可见容器，优先读取它的文字。
  function nearestChoiceText(control) {
    const choiceSelector =
      "input[type='radio'],input[type='checkbox'],[role='radio'],[role='checkbox']";
    let best = '';
    let p = control;
    for (let depth = 0; depth < 9 && p?.parentElement; depth++) {
      p = p.parentElement;
      if (p.matches("form,main,body,html")) break;
      const n = p.querySelectorAll(choiceSelector).length;
      if (n > 1) break;
      const t = norm(p).replace(/^[*＊]\s*/, '').trim();
      if (t && t.length <= 95) {
        // <label>内容可能是“女”，外层也可能有“选择女”；
        // 只取最小可见容器的文字，而不是 field 整体或 input.value。
        best = t;
        break;
      }
    }
    return best;
  }
  const optionTexts = [
    ...labelList,
    short(e.closest('label')),
    nearestChoiceText(e),
    short(e.getAttribute('aria-label') || ''),
    short(e.nextElementSibling),
    short(e.parentElement),
    short(e.getAttribute('value') || ''),
  ].map(short).filter(x => x && x.length <= 95);
  const option = isChoice ? (optionTexts[0] || '') : '';

  if (!isChoice) {
    add(e.getAttribute('aria-label'));
    for (const id of (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)) {
      add(document.getElementById(id));
    }
    labelList.forEach(add);
  }

  const titleSelector = [
    'legend', '.field-label', '.field-title', '.field-name', '.question-title',
    '.ant-form-item-label', '.form-item-label',
    '[class*="FieldTitle"]', '[class*="field-title"]',
    '[class*="FieldLabel"]', '[class*="field-label"]',
    '[class*="question-title"]', '[class*="questionTitle"]',
    '[class*="label-text"]', '[class*="LabelText"]',
    '[data-testid*="title"]', 'h2', 'h3', 'h4', '[role="heading"]',
  ].join(',');

  // 金数据这类问卷常用多层 div，题目并不一定在 <label> 里。
  // 从当前输入框逐层向上走，只看邻近的小范围容器，绝不以整张表单做题目。
  let branch = e;
  for (let depth = 0; depth < 11 && branch?.parentElement; depth++) {
    const parent = branch.parentElement;
    if (parent.matches('form,body,html,main')) break;
    const controls = parent.querySelectorAll(inputs);
    if (controls.length > 12) break;

    let prev = branch.previousElementSibling;
    for (let i = 0; prev && i < 3; i++, prev = prev.previousElementSibling) {
      // 跳过其他答案输入框（如“男”这项），优先取前面的题目文字。
      if (!prev.querySelector(inputs) && !prev.matches(inputs)) add(prev);
    }
    try {
      for (const node of Array.from(parent.querySelectorAll(titleSelector)).slice(0, 24)) {
        if (node.contains(e) || node.querySelector(inputs)) continue;
        if (node.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING) add(node);
      }
    } catch (_) {}
    // 即使站点 CSS 类名完全随机，也能从较小容器内的文字识别字段。
    const inner = norm(parent);
    if (controls.length <= 5 && inner.length && inner.length <= 70) {
      if (!isChoice || inner !== option) add(inner);
    }
    branch = parent;
  }

  add(e.getAttribute('placeholder') || '');
  add(e.getAttribute('name') || '');
  return {
    type, tag, role: e.getAttribute('role') || '',
    name: e.getAttribute('name') || '',
    label: candidates[0] || '',
    labels: candidates.slice(0, 18),
    option,
    optionCandidates: isChoice ? optionTexts.slice(0, 12) : [],
    placeholder: e.getAttribute('placeholder') || '',
    required: e.required || e.getAttribute('aria-required') === 'true',
    disabled: !!e.disabled,
  };
}"""

# 接受用户主动在窗口输入的任意 http/https 地址（包括 localhost 本地测试）。
# 不再监听剪贴板，也不再筛选金数据域名；拒绝 file:、javascript: 等协议。
LINK_RE = re.compile(r'''https?://[^\s<>"'，。；;！!）)\]】]+''', re.IGNORECASE)


def parse_urls(value: str) -> list[tuple[str, str]]:
    """从文本框提取可访问的 HTTP(S) 网址，重复网址自动去重。"""
    found = []
    seen = set()
    raw = (value or "").strip()
    for match in LINK_RE.finditer(raw):
        url = match.group(0).rstrip(".,;?，。；！")
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            continue
        # 端口也在标识里。用哈希避免两个路径末尾相同造成截图/题目清单重名。
        label = parts.path.strip("/").split("/")[-1] or "form"
        if parts.hostname.lower() == "jnu-cyxy.jinshuju.com" and label in FORMS:
            form_id = label
        else:
            fragment = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
            hostname = re.sub(r"[^a-zA-Z0-9_-]", "_", parts.netloc)[:34]
            form_id = f"{hostname}_{fragment}"
        if url not in seen:
            seen.add(url)
            found.append((form_id, url))
    return found


def normalize(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value), flags=re.UNICODE).lower()


def read_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    result = {
        "个人资料": dict(DEFAULT_CONFIG["个人资料"]),
        "自动提交": bool(raw.get("自动提交", False)),
        "自动确认最后进群选项": bool(raw.get("自动确认最后进群选项", True)),
        "每张问卷额外答案": dict(DEFAULT_CONFIG["每张问卷额外答案"]),
    }
    result["个人资料"].update(raw.get("个人资料", {}))
    result["每张问卷额外答案"].update(raw.get("每张问卷额外答案", {}))
    return result


def get_answer(label: str, form_id: str, config: dict, url: str = ""):
    """按问题标题匹配真实答案；空值不填，避免制造不真实的参与情况。"""
    question = normalize(label)
    if not question or len(question) > 80:
        return None
    # 统一资料区的固定选择优先于旧版曾保存的问卷自定义答案。
    fixed_values = {
        "是否为楼栋长、楼层长": {"是", "否"},
        "性别": {"男", "女"},
        "政治面貌": {
            "中共党员", "中共预备党员", "共青团员", "群众", "民主党派人士",
            "民主党派", "无党派人士", "少先队员", "其他",
        },
    }
    for key, permitted in fixed_values.items():
        if any(normalize(name) in question for name in ALIASES[key]):
            saved_choice = str(config.get("个人资料", {}).get(key, "")).strip()
            if saved_choice in permitted:
                return saved_choice

    forms_config = config.get("每张问卷额外答案", {})
    # 优先使用完整 URL 的专属答案；旧版金数据的表单 ID 依然有效。
    for config_key in ([url, form_id] if url else [form_id]):
        special = forms_config.get(config_key, {})
        for key in sorted(special, key=len, reverse=True):
            if normalize(key) and normalize(key) in question:
                value = special[key]
                return value if value not in ("", None, []) else None

    values = config.get("个人资料", {})
    for key, names in ALIASES.items():
        val = str(values.get(key, "")).strip()
        if not val:
            continue
        for name in sorted(names, key=len, reverse=True):
            word = normalize(name)
            if question == word or question.startswith(word) or (
                len(word) >= 3 and word in question
            ):
                return val
    return None


def save_question_list(page, form_id: str) -> Path:
    FIELD_DIR.mkdir(exist_ok=True)
    result = page.locator("input, textarea, select")
    records = []
    for idx in range(min(result.count(), 150)):
        try:
            element = result.nth(idx)
            meta = element.evaluate(FIELD_META_JS)
            if meta["type"] not in {"hidden", "submit", "reset", "button", "image"}:
                records.append(meta)
        except Exception:
            continue
    path = FIELD_DIR / f"{form_id}_题目清单.json"
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def canonical_political_name(value) -> str:
    """只转换明确同义的政治面貌叫法，不对普通词语做宽泛猜测。"""
    text = normalize(value)
    alternatives = {
        "中共党员": {"中共党员", "中国共产党党员", "共产党员"},
        "中共预备党员": {
            "中共预备党员", "中国共产党预备党员", "预备党员"
        },
        "共青团员": {"共青团员", "中国共产主义青年团团员"},
        "民主党派人士": {"民主党派人士", "民主党派"},
        "无党派人士": {"无党派人士", "无党派"},
        "群众": {"群众", "普通群众"},
    }
    for canonical, variants in alternatives.items():
        if text in variants:
            return canonical
    return text


def option_match_score(expected, candidate: str) -> int:
    """候选答案评分。100 为准确，80+ 为“含/包括”语义等价；0 为不匹配。

    不使用任意子串匹配，以免“中共党员”错误命中“中共预备党员”，
    或“是”错误命中“不是”。对明确“含预备党员”的统称予以兼容。
    """
    actual = normalize(candidate)
    wanted = normalize(expected)
    if not actual or not wanted:
        return 0
    if actual == wanted:
        return 100
    if canonical_political_name(actual) == canonical_political_name(wanted):
        return 96  # 例如“民主党派人士”与“民主党派”

    # 只放宽对包含型说明的容忍度，如 中共党员（含预备党员）。
    candidate_text = str(candidate).strip()
    expected_text = str(expected).strip()
    inclusive_prefixes = ("含", "包含", "包括", "以及", "及", "含有")

    def bracket_base(text):
        match = re.fullmatch(r"\s*(.+?)\s*[（(]([^()（）]+)[）)]\s*", text)
        if match:
            return normalize(match.group(1)), normalize(match.group(2))
        return None, None

    base, note = bracket_base(candidate_text)
    if base and note and any(note.startswith(p) for p in inclusive_prefixes):
        # 用户个人资料是“中共党员”，网页写“中共党员（含预备党员）”。
        if canonical_political_name(base) == canonical_political_name(wanted):
            return 88
        # 若用户是预备党员，明确写“含预备党员”的统称也可以兼容。
        if (canonical_political_name(wanted) == "中共预备党员"
                and canonical_political_name(base) in {"中共党员", "党员"}
                and any(term in note for term in ("含预备党员", "包含预备党员",
                                                 "包括预备党员", "以及预备党员"))):
            return 82

    # 也允许没有括号的“中共党员，含预备党员”。
    if actual.startswith(wanted):
        extra = actual[len(wanted):]
        if extra and any(extra.startswith(p) for p in inclusive_prefixes):
            return 84

    # 用户保存时填入完整说明，而网页只有基本名称时，降级到基本名称。
    preferred_base, preferred_note = bracket_base(expected_text)
    if (canonical_political_name(preferred_base) == canonical_political_name(actual)
            and preferred_note and any(
                preferred_note.startswith(p) for p in inclusive_prefixes
            )):
        return 78

    return 0


def _radio_group_key(meta: dict, matched_label: str) -> str:
    return meta.get("name") or normalize(matched_label) or "unknown"


def click_native_radio(el) -> tuple[bool, str]:
    """兼容原生 input、隐藏 input + 自定义可点击行，操作后回读验证。"""
    try:
        if el.is_checked():
            return True, "已选中"
    except Exception:
        return False, "不是可操作的原生单选框"

    errors = []

    # 可见的原生控件，优先按普通 Playwright 方式操作。
    try:
        if el.is_visible():
            el.check(timeout=1100)
            if el.is_checked():
                return True, "input.check()"
    except Exception as exc:
        errors.append("input：" + str(exc).splitlines()[0][:95])

    # <label for="id"> / <label><input ...></label> 是浏览器语义关联。
    try:
        labels = el.evaluate("""e => {
          for (const l of Array.from(e.labels || [])) {
            const r=l.getBoundingClientRect();
            if (r.width>0 && r.height>0) {
                l.setAttribute("data-assistant-radio-click", "label");
                return true;
            }
          }
          return false;
        }""")
        if labels:
            label_locator = el.locator(
                "xpath=ancestor::label[@data-assistant-radio-click]"
            )
            if label_locator.count():
                label_locator.first.click(timeout=1800)
            else:
                # <label for="id"> 可能位于外层，不是 input 的祖先；
                # 仍可通过原生 label.click() 正确触发浏览器选择事件。
                el.evaluate("""e => {
                  const target = Array.from(e.labels || []).find(label => {
                    const r=label.getBoundingClientRect();
                    return r.width>0&&r.height>0;
                  });
                  if (target) target.click();
                }""")
            if el.is_checked():
                return True, "label.click()"
    except Exception as exc:
        errors.append("label：" + str(exc).splitlines()[0][:95])

    # 最关键的金数据变体：一个可见的“选项行”包含隐藏 input，
    # 视觉上的字在 input 的兄弟元素里，点击整行触发 React onClick。
    try:
        has_parent = el.evaluate("""e => {
          const choice = "input[type='radio'],input[type='checkbox'],[role='radio'],[role='checkbox']";
          let p=e.parentElement;
          for (let depth=0;depth<9 && p;depth++,p=p.parentElement) {
             if (p.matches('form,main,body,html')) break;
             if (p.querySelectorAll(choice).length > 1) break;
             const r = p.getBoundingClientRect();
             const style=getComputedStyle(p);
             const txt=(p.innerText||'').replace(/\\s+/g,' ').trim();
             if (r.width>5 && r.height>5 && style.display!=='none' &&
                 style.visibility!=='hidden' && txt && txt.length<=110) {
                 p.setAttribute("data-assistant-radio-row-click","1");
                 return true;
             }
          }
          return false;
        }""")
        if has_parent:
            clickable = el.locator(
                "xpath=ancestor::*[@data-assistant-radio-row-click='1'][1]"
            )
            clickable.click(timeout=2400)
            if el.is_checked():
                return True, "可见选项行.click()"
    except Exception as exc:
        errors.append("选项行：" + str(exc).splitlines()[0][:100])

    try:
        el.check(force=True, timeout=1200)
        if el.is_checked():
            return True, "input.check(force=True)"
    except Exception as exc:
        errors.append("强制选中：" + str(exc).splitlines()[0][:95])

    return False, "; ".join(errors[-2:]) or "已尝试但仍未选中"


# 兜底定位：有些金数据模板的 input 没有 name/label，可直接从可视的
# “性别”“政治面貌”标题找到对应题目容器，再识别其单选项。
FOCUSED_RADIOS_JS = r"""() => {
  const ctl = "input[type='radio'],[role='radio']";
  const norm = t => String(t||"").replace(/[\s：:*＊，,、]/g,"").trim();
  const titleNames = {
    "性别":["性别","学生性别","您的性别","你的性别"],
    "政治面貌":["政治面貌","政治身份","党团身份","政治情况"],
  };
  const byTitle = t => {
    for (const [key,arr] of Object.entries(titleNames)) {
      if (arr.some(s=>t===s || t===s+"必填" || t===s+"请选择")) return key;
    }
    return null;
  };
  const ownText = e => {
    const pieces=[];
    for (const n of e.childNodes || []) {
      if (n.nodeType===3) pieces.push(n.nodeValue);
    }
    return norm(pieces.join(" "));
  };
  const headings=[];
  for (const e of document.querySelectorAll(
    "label,legend,span,strong,div,p,h2,h3,h4,h5,[role='heading']"
  )) {
    const category=byTitle(ownText(e));
    if (category) headings.push({category,element:e});
  }
  const result=[];
  for (const field of ["性别","政治面貌"]) {
    const anchors=headings.filter(x=>x.category===field);
    let scope=null;
    for (const anchor of anchors) {
      let p=anchor.element;
      for (let depth=0;depth<13&&p?.parentElement;depth++) {
        p=p.parentElement;
        if (p.matches("form,main,body,html,[role='main']")) break;
        const radios=p.querySelectorAll(ctl);
        if (!radios.length || radios.length>12) continue;
        const differentTitle=headings.some(x=>
          x.category!==field && p.contains(x.element)
        );
        if (differentTitle) continue;
        scope=p;
        break;
      }
      if (scope) break;
    }
    if (!scope) {
      result.push({field,found:false,options:[]});
      continue;
    }
    const options=[];
    const radios=[...scope.querySelectorAll(ctl)];
    let id=0;
    for (const input of radios) {
      const rawType=input.getAttribute("type")||"";
      const isNative=input.tagName.toLowerCase()==="input";
      if (input.disabled) continue;
      const texts=[];
      const add=t=>{
        const txt=String(t||"").replace(/\s+/g," ").trim();
        // 单选控件的 HTML label 有时指向整题标题，不应把“性别”
        // 误当成“男/女”的答案文字。
        const compact=txt.replace(/[\s：:*＊，,、]/g,"");
        if (compact === field || compact === field+"必填") return;
        if (txt && txt.length<=110&&!texts.includes(txt)) texts.push(txt);
      };
      if (isNative) {
        for (const label of input.labels||[]) add(label.innerText);
      } else {
        add(input.innerText);
      }
      add(input.getAttribute("aria-label"));
      let parent=input.parentElement;
      for (let depth=0; depth<8&&parent;depth++,parent=parent.parentElement) {
        if (parent.querySelectorAll(ctl).length>1) break;
        add(parent.innerText);
        if (texts.length>0) break;
      }
      const visibleLabel=texts[0] || "";
      input.setAttribute("data-assistant-focused",field+"-"+(++id));
      options.push({
        id, label:visibleLabel, rawValue:input.getAttribute("value")||"",
        name:input.getAttribute("name")||"",
        checked:isNative?!!input.checked:input.getAttribute("aria-checked")==="true",
        native:isNative,
      });
    }
    result.push({field,found:true,options});
  }
  return result;
}"""


def fix_gender_and_political(page, config: dict, notify,
                             form_id: str = "") -> tuple[int, list[dict]]:
    """基于可视题目标题再次核对两个关键单选题，失败要明确展示诊断。"""
    try:
        groups=page.evaluate(FOCUSED_RADIOS_JS)
    except Exception as exc:
        notify(f"性别/政治面貌专项检测失败：{str(exc)[:140]}")
        return 0, []

    changes=0
    details=[]
    for group in groups:
        field=group["field"]
        answer=str(config.get("个人资料", {}).get(field, "")).strip()
        record={
            "题目":field,"已设置":bool(answer),
            "控件定位成功":bool(group.get("found")),
            "候选项":[o["label"] for o in group.get("options",[])],
            "控件名称":list(dict.fromkeys(
                o.get("name", "") for o in group.get("options",[])
                if o.get("name")
            )),
            "匹配结果":"未操作",
        }
        if not answer:
            notify(f"{field}尚未在软件窗口设置，请先选择并保存。")
            record["匹配结果"]="资料未设置"
            details.append(record)
            continue
        if not group.get("found"):
            notify(f"尚未从网页结构中定位“{field}”所在的单选题。")
            record["匹配结果"]="未识别到题目容器"
            details.append(record)
            continue

        scores=[
            (option_match_score(answer,o.get("label","")),o)
            for o in group.get("options",[])
        ]
        good=[(score,o) for score,o in scores if score>0]
        if not good:
            notify(f"{field}：网页选项与保存的答案未能匹配，未做猜测。")
            record["匹配结果"]="候选项没有可靠匹配"
            details.append(record)
            continue

        high=max(score for score,_ in good)
        winners=[o for score,o in good if score==high]
        if len(winners)!=1:
            notify(f"{field}：有多个分数相同的候选项，为避免误选，已跳过。")
            record["匹配结果"]="存在匹配歧义"
            details.append(record)
            continue

        option=winners[0]
        selected=page.locator(
            f"[data-assistant-focused='{field}-{option['id']}']"
        )
        if not selected.count():
            record["匹配结果"]="目标选项在页面更新后失效"
            details.append(record)
            continue

        try:
            if option["native"]:
                original=selected.first.is_checked()
                if original:
                    ok,method=True,"已选中"
                else:
                    ok,method=click_native_radio(selected.first)
            else:
                original=selected.first.get_attribute("aria-checked")=="true"
                if not original:
                    selected.first.click(timeout=2900)
                ok=selected.first.evaluate("""e =>
                  e.getAttribute("aria-checked")==="true" ||
                  !!e.querySelector("input:checked") ||
                  e.classList.contains("checked") ||
                  e.classList.contains("selected") ||
                  e.getAttribute("data-state")==="checked"
                """)
                method="自定义单选控件.click()"

            record["匹配结果"]="已选中" if ok else "尝试后未选中"
            record["匹配方式"]="精确" if high==100 else f"容错({high})"
            record["操作方式"]=method
            record["实际选项"]=option.get("label","")
            if ok:
                if not original:
                    changes += 1
                notify(f"{field}已核实选中：“{option['label']}”。")
            else:
                notify(f"{field}已尝试点击，但仍未检测到选中状态，请查看诊断文件。")
        except Exception as exc:
            record["匹配结果"]="点击失败"
            record["错误"]=str(exc)[:160]
            notify(f"{field}点击失败：{str(exc)[:120]}")
        details.append(record)

    if form_id:
        try:
            FIELD_DIR.mkdir(exist_ok=True)
            target=FIELD_DIR/f"{form_id}_性别政治面貌诊断.json"
            target.write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding="utf-8")
            notify(f"已保存性别与政治面貌诊断：{target.name}")
        except Exception:
            pass
    return changes,details


def best_radio_scores(page, form_id: str, config: dict, url: str = "") -> dict:
    """先扫描同一组全部单选项，确保精准选项优先于容错选项。"""
    result = {}
    entries = page.locator("input[type='radio'],[role='radio']")
    for index in range(min(entries.count(), 160)):
        try:
            element = entries.nth(index)
            meta = element.evaluate(FIELD_META_JS)
            if meta.get("disabled"):
                continue
            answer, matched_label = choose_answer(meta, form_id, config, url)
            if answer is None:
                continue
            candidates = meta.get("optionCandidates") or [meta.get("option", "")]
            score = max(
                (option_match_score(answer, text) for text in candidates),
                default=0,
            )
            if score <= 0:
                continue
            key = (_radio_group_key(meta, matched_label), normalize(answer))
            result[key] = max(score, result.get(key, 0))
        except Exception:
            continue
    return result


def choose_answer(meta: dict, form_id: str, config: dict, url: str):
    """优先匹配输入框真实题目；多个候选题目时逐一查找。"""
    labels = meta.get("labels") or [meta.get("label", "")]
    for label in labels:
        answer = get_answer(label, form_id, config, url)
        if answer is not None:
            return answer, label
    return None, meta.get("label", "")


def fill_form(page, form_id: str, config: dict, url: str = "") -> tuple[int, list[str], list[dict]]:
    """填写且回读校验；返回诊断详情，填不进去会明确记录原因。"""
    count_filled = 0
    pending = set()
    reports = []
    # 选中单选项后可能展开新问题，允许进行两轮处理。
    for round_index in range(2):
        priority = best_radio_scores(page, form_id, config, url)
        elements = page.locator(
            "input,textarea,select,[role='radio'],[role='checkbox']"
        )
        n = min(elements.count(), 180)
        for idx in range(n):
            try:
                el = elements.nth(idx)
                meta = el.evaluate(FIELD_META_JS)
                kind = meta["type"]
                role = meta.get("role", "")
                if kind in {"hidden", "submit", "button", "reset", "image", "file", "password"}:
                    continue
                if meta["disabled"]:
                    continue
                answer, matched_label = choose_answer(meta, form_id, config, url)
                if answer is None:
                    if round_index == 0 and (el.is_visible() or kind in {"radio", "checkbox"}):
                        pending.add(meta["label"] or f"第{idx+1}项({kind})")
                    continue

                report = {
                    "题目": matched_label, "控件": kind, "选项": meta["option"],
                    "结果": "未填", "轮次": round_index + 1,
                }
                if kind in {"radio", "checkbox"} or role in {"radio", "checkbox"}:
                    choices = answer if isinstance(answer, list) else [answer]
                    option_candidates = meta.get("optionCandidates") or [
                        meta.get("option", "")
                    ]
                    matches = [
                        (option_match_score(item, candidate), candidate)
                        for item in choices for candidate in option_candidates
                    ]
                    score, selected_text = max(
                        matches, key=lambda pair: pair[0],
                        default=(0, ""),
                    )
                    if score == 0:
                        continue
                    report["选项"] = selected_text
                    if kind == "radio" or role == "radio":
                        best = priority.get(
                            (_radio_group_key(meta, matched_label), normalize(answer)),
                            0,
                        )
                        if score < best:
                            # 比如两个选项：“中共党员（含预备党员）”“中共党员”，
                            # 必须优先选后面的准确选项，而非按页面先后随意选择。
                            continue
                    report["匹配方式"] = (
                        "精确匹配" if score == 100 else f"含义容错匹配（得分 {score}）"
                    )
                    if role in {"radio", "checkbox"} and kind not in {"radio", "checkbox"}:
                        already = el.get_attribute("aria-checked") == "true"
                        if not already:
                            el.click(timeout=3500)
                        success = el.get_attribute("aria-checked") == "true"
                    else:
                        already = el.is_checked()
                        if already:
                            success, method = True, "已选中"
                        else:
                            success, method = click_native_radio(el)
                        report["操作方式"] = method
                    report["结果"] = "已勾选" if success else "勾选失败"
                    if success and not already:
                        count_filled += 1
                        pending.discard(matched_label)
                elif meta["tag"] == "select":
                    if not el.is_visible():
                        continue
                    current = el.input_value()
                    options = el.evaluate("""e => Array.from(e.options).map(o => ({
                        text:o.textContent.trim(), value:o.value, disabled:o.disabled
                    }))""")
                    available = sorted([
                        (option_match_score(answer, o["text"]), o)
                        for o in options if not o["disabled"]
                    ], key=lambda pair: pair[0], reverse=True)
                    if available and available[0][0] > 0:
                        score, best_option = available[0]
                        if current != best_option["value"]:
                            el.select_option(value=best_option["value"], timeout=2500)
                        success = el.input_value() == best_option["value"]
                        report["匹配方式"] = (
                            "精确匹配" if score == 100 else f"含义容错匹配（得分 {score}）"
                        )
                    else:
                        success = False
                        report["匹配方式"] = "无对应下拉选项"
                    report["结果"] = "已选择" if success else "选择失败"
                    if success:
                        pending.discard(matched_label)
                        if not current:
                            count_filled += 1
                elif kind in {"text", "tel", "email", "number", "date", "time", "search",
                              "url", "textarea"} or meta["tag"] == "textarea":
                    if not el.is_visible():
                        report["结果"] = "元素不可见"
                    else:
                        current = el.input_value()
                        if not str(current).strip():
                            el.fill(str(answer), timeout=3500)
                            el.dispatch_event("change")
                        after = el.input_value()
                        success = str(after).strip() == str(answer).strip()
                        report["结果"] = "已填且校验通过" if success else "填写后值未匹配"
                        if success and not str(current).strip():
                            count_filled += 1
                            pending.discard(matched_label)
                else:
                    report["结果"] = "暂不支持的控件"
                if round_index == 0 or report["结果"] != "未填":
                    reports.append(report)
                if len(reports) >= 160:
                    break
            except Exception as exc:
                if len(reports) < 160:
                    reports.append({
                        "题目": meta.get("label", "?") if "meta" in locals() else "?",
                        "控件": meta.get("type", "?") if "meta" in locals() else "?",
                        "结果": "失败: " + str(exc)[:190],
                    })
                continue
        page.wait_for_timeout(420)
    return count_filled, sorted(pending), reports

def missing_required(page) -> list[str]:
    try:
        return page.evaluate("""() => {
          const empty = [];
          const checkedGroups = new Set();
          for (const e of document.querySelectorAll(
            "input[required], textarea[required], select[required], [aria-required='true']"
          )) {
            const r = e.getBoundingClientRect();
            if (!r.width || !r.height || e.disabled) continue;
            const type = (e.type || "").toLowerCase();
            const title = e.getAttribute("aria-label") || e.getAttribute("placeholder") ||
              e.getAttribute("name") || type || "未命名必填项";
            if (type === "radio") {
              if (checkedGroups.has(e.name)) continue;
              checkedGroups.add(e.name);
              if (!document.querySelector(`input[type="radio"][name="${CSS.escape(e.name)}"]:checked`))
                empty.push(title);
            } else if (type === "checkbox") {
              if (!e.checked) empty.push(title);
            } else if (!String(e.value || "").trim()) empty.push(title);
          }
          return empty.slice(0, 20);
        }""")
    except Exception:
        return []


def looks_submitted(page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=4000)[:7000]
        return any(s in body for s in [
            "提交成功", "报名成功", "填写成功", "提交完成",
            "您的信息已提交", "感谢您的填写", "感谢参与",
            "Your response has been recorded", "Thank you for submitting",
            "Submission successful", "Form submitted", "Thank you for your response",
        ])
    except Exception:
        return False


def submit_form(page) -> bool:
    """点击页面实际存在的提交按钮；从不绕过验证码或登录。"""
    if looks_submitted(page):
        return True
    candidates = [
        page.get_by_role("button", name=re.compile(
            r"^(提\s*交|确认提交|立即报名|确认报名|提交报名|Submit|Send|Finish|Confirm)$",
            flags=re.IGNORECASE,
        )),
        page.locator("button[type='submit'], input[type='submit']"),
        page.locator("button").filter(has_text=re.compile(r"提交|报名|Submit|Send", re.IGNORECASE)),
    ]
    for group in candidates:
        try:
            n = min(group.count(), 8)
            for idx in range(n):
                btn = group.nth(idx)
                if btn.is_visible() and btn.is_enabled():
                    btn.click(timeout=3500)
                    page.wait_for_timeout(2000)
                    return looks_submitted(page)
        except Exception:
            continue
    raise RuntimeError("没有找到可点击的提交按钮，请在浏览器里手动提交。")


JOIN_WORDS = (
    "进群", "入群", "加群", "群聊", "微信群", "qq群", "扫码进",
    "扫码入", "志愿群聊", "群二维码", "进群截图", "群截图",
)

# 这是用户反馈后改为“按单选题真实顺序”的方案。
# 在金数据问卷末尾，倒数第二组是时间单选题，最后一组是带有进群图片的单选题。
# 所有操作仅在【最后组确实属于群聊/扫码】且【倒数第二组确实属于时间】时执行；
# 没有明确证据就留空，避免误选性别/楼栋长等前面的题目。
LAST_TWO_RADIO_GROUPS_JS = r"""() => {
  const normalize = text => String(text || "").replace(/\s+/g," ").trim();
  const inputs = [...document.querySelectorAll(
    "input[type='radio'],[role='radio']"
  )].filter(el =>
    !el.disabled &&
    !el.closest("aside,nav,header,footer,[role='navigation']"));
  const groupMap = new Map();
  const containers = new WeakMap();
  let anonymous = 0;
  let itemIndex = 0;

  for (const input of inputs) {
    let clickTarget = input;
    if (input.tagName.toLowerCase() === "input") {
      let label = input.closest("label");
      if (!label && input.id) {
        try {
          label = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
        } catch (_) {}
      }
      if (label) clickTarget = label;
    }
    const rect = clickTarget.getBoundingClientRect();
    const css = window.getComputedStyle(clickTarget);
    if (rect.width < 1 || rect.height < 1 ||
        css.visibility === "hidden" || css.display === "none") continue;

    let label = normalize(clickTarget.innerText || "");
    if (!label) label = normalize(input.getAttribute("aria-label") || "");
    if (!label && input.labels?.length) {
      label = normalize(input.labels[0].innerText);
    }
    if (!label) label = normalize(input.nextElementSibling?.innerText || "");
    if (!label) label = normalize(input.parentElement?.innerText || "");
    if (!label) label = normalize(input.value || "");

    let key = "";
    const name = (input.getAttribute("name") || "").trim();
    const fieldContainer = input.closest([
      "[role='radiogroup']", "fieldset", "[data-field-id]",
      "[data-question-id]", ".field-container", ".field",
      ".question", ".form-item", "[class*='field-container']",
      "[class*='FieldContainer']", "[class*='question-container']"
    ].join(","));
    if (name) key = "name:" + name;
    else if (fieldContainer) {
      if (!containers.has(fieldContainer)) containers.set(fieldContainer, "scope:" + ++anonymous);
      key = containers.get(fieldContainer);
    } else {
      const rg = input.closest("[role='group']");
      if (rg) {
        if (!containers.has(rg)) containers.set(rg, "group:" + ++anonymous);
        key = containers.get(rg);
      } else {
        // 无 name、无明确题目容器时无法安全确定题目分组，暂不乱选。
        continue;
      }
    }

    let title = "";
    if (fieldContainer) {
      const heading = fieldContainer.querySelector(
        "legend, .field-label, .field-title, .question-title,"+
        "[class*='field-title'],[class*='FieldTitle'],h3,h4"
      );
      title = normalize(heading?.innerText || "");
      if (!title) title = normalize(fieldContainer.innerText || "").slice(0,280);
    }

    const checked = input.tagName.toLowerCase() === "input"
      ? Boolean(input.checked)
      : (input.getAttribute("aria-checked") === "true");
    const id = ++itemIndex;
    clickTarget.setAttribute("data-last-two-radio-option", String(id));
    if (!groupMap.has(key)) {
      groupMap.set(key, {
        key, title, options: [],
        pos: id,
      });
    }
    const group = groupMap.get(key);
    if (title && !group.title) group.title = title;
    group.options.push({
      id, label: label.slice(0,125), checked,
      inputType: input.getAttribute("type") || input.getAttribute("role") || "",
    });
  }
  return [...groupMap.values()]
    .sort((a,b) => a.pos - b.pos)
    .map(({key,title,options,pos}) => ({key,title,options,pos}));
}"""


def looks_like_time_group(group: dict) -> bool:
    text = " ".join([
        group.get("title", ""),
        *(x.get("label", "") for x in group.get("options", [])),
    ])
    return bool(re.search(
        r"时间|时段|活动日期|志愿时|报名时|场次|"
        r"\d{1,2}\s*月\s*\d{1,2}\s*日|"
        r"\d{1,2}\s*[:：]\s*\d{2}|"
        r"周[一二三四五六日天]|星期[一二三四五六日天]|上午|下午|晚上",
        text, flags=re.IGNORECASE,
    ))


def looks_like_group_image_question(group: dict) -> bool:
    text = " ".join([
        group.get("title", ""),
        *(x.get("label", "") for x in group.get("options", [])),
    ]).lower()
    return any(word in text for word in JOIN_WORDS)


def choose_radio_option(page, group: dict, notify, description: str) -> tuple[bool, str]:
    """只操作实际存在的组内可选标签；选中后回读原生 input，避免点击无效。"""
    options = group.get("options", [])
    if not options:
        notify(f"{description}没有单选选项。")
        return False, ""

    existing = next((o for o in options if o.get("checked")), None)
    if existing:
        label = existing.get("label", "")
        notify(f"{description}已经选中：“{label}”，保留原选择。")
        return True, label

    for option in options:
        element = page.locator(f"[data-last-two-radio-option='{option['id']}']")
        try:
            if not element.count():
                continue
            element.first.scroll_into_view_if_needed(timeout=2800)
            try:
                element.first.click(timeout=2700)
            except Exception:
                # 有些金数据模板将真正的 input 隐藏在图文标签内。
                native = element.first.locator("input[type='radio']")
                if native.count():
                    native.first.check(force=True, timeout=2500)
                else:
                    element.first.check(force=True, timeout=2500)
            page.wait_for_timeout(200)
            try:
                verified = element.first.evaluate("""e => {
                  const input = e.matches("input[type='radio']") ? e
                    : e.querySelector("input[type='radio']");
                  return input ? !!input.checked :
                    (e.getAttribute("aria-checked") === "true" ||
                     !!e.querySelector('[aria-checked="true"]') ||
                     e.classList.contains("checked") ||
                     e.classList.contains("selected") ||
                     e.getAttribute("data-state") === "checked");
                }""", timeout=1700)
            except Exception:
                verified = False
            if not verified:
                # React 重绘可能会替换标签，旧定位属性随之消失，重新识别分组。
                try:
                    updated = page.evaluate(LAST_TWO_RADIO_GROUPS_JS)
                    matching = next(
                        (g for g in updated if g["key"] == group["key"]), None
                    )
                    verified = bool(matching and any(
                        x.get("checked") and x.get("label") == option.get("label")
                        for x in matching.get("options", [])
                    ))
                except Exception:
                    pass
            if verified:
                label = option.get("label", "")
                notify(f"{description}已自动选中：“{label}”。")
                return True, label
            notify(f"{description}已点击但未确认选中：“{option.get('label','')}”，请检查网页。")
        except Exception as exc:
            notify(f"{description}点击失败：{str(exc)[:140]}")
    return False, ""


def auto_select_last_two_radio_groups(page, notify, form_id: str = "") -> dict:
    """按顺序选择最后两个单选题。时间选首项；进群图选首项；已选项不覆盖。"""
    result = {
        "time_selected": False, "group_selected": False,
        "time_label": "", "group_label": "",
    }
    try:
        groups = page.evaluate(LAST_TWO_RADIO_GROUPS_JS)
    except Exception as exc:
        notify(f"最后两题识别失败：{str(exc)[:160]}")
        return result

    if form_id:
        try:
            FIELD_DIR.mkdir(exist_ok=True)
            (FIELD_DIR / f"{form_id}_最后两个单选题诊断.json").write_text(
                json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    if len(groups) < 2:
        notify(f"仅识别出 {len(groups)} 组单选题，无法确认最后两题，未自动勾选。")
        return result
    time_group, group_image = groups[-2], groups[-1]

    # 对金数据“11月24日（周二）17:30-19:00”“志愿群聊”这两个答案有效。
    # 不是目标题目就不把其他单选题（如性别/楼栋长）错误选中。
    if not looks_like_group_image_question(group_image):
        notify("最后一组单选题没有识别到进群/群聊信息，未擅自选择。")
        return result
    if not looks_like_time_group(time_group):
        notify("倒数第二组单选题不是明显的活动时间题，未擅自选择。")
        return result

    result["time_selected"], result["time_label"] = choose_radio_option(
        page, time_group, notify, "活动时间"
    )
    result["group_selected"], result["group_label"] = choose_radio_option(
        page, group_image, notify, "进群图片选项"
    )
    return result


# 为每张问卷的最后一个问题定位内嵌图片，而不是提交后的页面二维码。
# 通过最后一个表单控件确定内容列和题目区域，排除右侧的“扫码分享给好友”。
LAST_QUESTION_IMAGES_JS = r"""() => {
  const inputSelector = "input,textarea,select,[role='radio'],[role='checkbox']";
  const valid = (e) => {
    const style = getComputedStyle(e);
    const r = e.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && r.width > 0 && r.height > 0;
  };
  const controls = Array.from(document.querySelectorAll(inputSelector))
    .filter(e => valid(e) && ![
      'hidden', 'submit', 'button', 'reset', 'password', 'file'
    ].includes((e.getAttribute('type') || '').toLowerCase()));
  const last = controls.at(-1);
  if (!last) return {reason:'页面没有可见问卷控件', selected:[]};
  const lastRect=last.getBoundingClientRect();
  const center = r => r.left + r.width/2;
  const bottom = r => r.top + r.height;
  const absY = r => r.top + window.scrollY;

  // 尽量找到表单正文列，不让固定在右边的分享二维码参与匹配。
  let main = last.closest('form');
  if (!main) {
    for (let p=last.parentElement; p && p !== document.body; p=p.parentElement) {
      const r=p.getBoundingClientRect();
      if (r.width>=320 && r.width<=Math.max(500,window.innerWidth*0.86)
          && p.querySelectorAll(inputSelector).length>=2) {
        main=p;
        break;
      }
    }
  }
  const mainRect=(main || last.parentElement || last).getBoundingClientRect();
  const leftBound=mainRect.left-35;
  const rightBound=mainRect.right+35;
  const containsInputCount = e => e.querySelectorAll?.(inputSelector).length || 0;
  const shareText = /分享给好友|扫码分享|分享表单|share this|share form|分享此表单/i;
  const groupText = /进群|入群|扫码|加群|群聊|微信群|QQ群|群二维码|join group|qr code/i;
  const isLocalShare = (el) => {
    let p=el;
    for (let i=0;i<3 && p?.parentElement;i++) {
      p=p.parentElement;
      const t=(p.innerText||'').trim();
      if (t.length<140 && shareText.test(t)) return true;
    }
    return false;
  };
  const rank=[];
  const nodes=Array.from(document.querySelectorAll('img,canvas,svg'));
  for (const el of nodes) {
    if (el.tagName.toLowerCase()==='svg' && el.ownerSVGElement) continue;
    if (!valid(el) || isLocalShare(el)) continue;
    if (el.closest('aside,nav,header,footer,[role="navigation"]')) continue;
    const r=el.getBoundingClientRect();
    if (r.width<90 || r.height<90 || r.width>1500 || r.height>2000) continue;
    const cx=center(r);
    if (cx < leftBound || cx > rightBound) continue;
    if (Math.abs(absY(r)-absY(lastRect)) > 2400) continue;
    let score=25;
    if (main?.contains(el)) score+=55;
    let common=false;
    for (let p=last.parentElement,depth=0; p && depth<9; p=p.parentElement,depth++) {
      if (p.contains(el)) {
        const ct=containsInputCount(p);
        if (ct<=8 && p.getBoundingClientRect().height<2000) {
          score+=180-depth*9;
          common=true;
        }
        break;
      }
    }
    // 更靠近最后一题的图片更可靠。
    score += Math.max(0, 95-Math.abs(absY(r)-absY(lastRect))/16);
    const details=[
      el.getAttribute('alt')||'', el.getAttribute('title')||'',
      el.getAttribute('data-description')||'',
    ].join(' ');
    if (groupText.test(details)) score+=70;
    if (shareText.test(details)) continue;
    let localText='';
    for (let p=el.parentElement, depth=0; p && depth<4; p=p.parentElement,depth++) {
      const t=(p.innerText||'').trim();
      if (t.length>5 && t.length<400) {localText=t;break;}
    }
    if (groupText.test(localText)) score+=50;
    const groupHint = groupText.test(localText) || groupText.test(details);
    const inLastHalf = absY(r) >= absY(mainRect) + mainRect.height*0.50;
    const nearLast = Math.abs(absY(r)-absY(lastRect)) < 900;
    rank.push({
      el,score,common,groupHint,inLastHalf,nearLast,
      tag:el.tagName.toLowerCase(),hint:details.slice(0,100),
    });
  }
  rank.sort((a,b)=>b.score-a.score);
  // 如果不在最后题容器内，要求距最后的交互项足够近；防止截到页头宣传图。
  const selected=rank.filter(v=>
    v.common || (v.score>=120 && v.inLastHalf && v.nearLast && v.groupHint)
  ).slice(0,3);
  selected.forEach((v,i)=>v.el.setAttribute('data-form-last-image',String(i+1)));
  return {
    reason:selected.length? '已找到最后一题附近的图片':'未找到最后一题内可保存的图片',
    selected:selected.map((v,i)=>({id:i+1,score:v.score,tag:v.tag,hint:v.hint}))
  };
}"""


def capture_last_question_images(page, form_id: str, label: str, notify) -> int:
    """提交前仅保存问卷末尾题目中的真实图片，不存提交成功页。"""
    PICTURE_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = safe_filename(f"{label}_{form_id}_{stamp}")
    try:
        last = page.locator(
            "input:not([type='hidden']):not([type='submit']),"
            "textarea,select,[role='radio'],[role='checkbox']"
        ).last
        if last.count():
            last.scroll_into_view_if_needed(timeout=4500)
            page.wait_for_timeout(550)
    except Exception:
        pass
    try:
        result = page.evaluate(LAST_QUESTION_IMAGES_JS)
    except Exception as exc:
        notify(f"识别最后一道题的图片失败：{str(exc)[:160]}")
        return 0
    count = 0
    for image_info in result.get("selected", []):
        element = page.locator(f"[data-form-last-image='{image_info['id']}']")
        filename = PICTURE_DIR / f"{prefix}_最后一题进群图片_{image_info['id']}.png"
        try:
            element.first.screenshot(path=str(filename), timeout=9000)
            count += 1
            notify(f"已保存问卷最后一题的图片：{filename.name}")
        except Exception as exc:
            notify(f"保存题目图片失败：{str(exc)[:110]}")
    if not count:
        notify("最后一道题附近未找到可保存的图片；不会再用提交后截图或分享二维码代替。")
    return count


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name)[:65]




class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = read_config()
        self.tasks = queue.Queue()  # ("open", form_id, url) / ("submit", job_id) / ("skip", job_id)
        self.ui_events = queue.Queue()
        self.review_items = {}  # tkinter 主线程管理显示；Playwright 对象只在工作线程内
        self.closed = False
        self.url_var = tk.StringVar()
        self.review_var = tk.StringVar()
        self.active_custom_url = None
        self.profile_vars = {
            key: tk.StringVar(value=str(self.config["个人资料"].get(key, "")))
            for key in PROFILE_FIELDS
        }
        self.auto_var = tk.BooleanVar(value=self.config["自动提交"])
        self.join_var = tk.BooleanVar(value=self.config["自动确认最后进群选项"])

        root.title("通用问卷自动填报助手｜粘贴网址后点击开始")
        root.geometry("920x840")
        root.minsize(780, 740)

        header = ttk.Frame(root, padding=14)
        header.pack(fill="x")
        ttk.Label(header, text="通用问卷自动填报助手", font=("Microsoft YaHei", 17, "bold")).pack(anchor="w")
        ttk.Label(
            header, text="填写个人资料 → 在下方粘贴问卷网址 → 点击“开始自动填写” → 检查答案并提交 → 保存进群图片",
            wraplength=860,
        ).pack(anchor="w", pady=(6, 0))

        url_box = ttk.LabelFrame(root, text="问卷网址（支持任意 HTTP/HTTPS 站点）", padding=10)
        url_box.pack(fill="x", padx=14, pady=5)
        ttk.Label(
            url_box, text="把自己想测试的链接粘贴到这里。软件不会监控剪贴板，只有点击按钮才会打开网页。",
            wraplength=840,
        ).pack(anchor="w")
        input_row = ttk.Frame(url_box)
        input_row.pack(fill="x", pady=(8, 1))
        self.url_entry = ttk.Entry(input_row, textvariable=self.url_var, font=("Consolas", 11))
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.url_entry.bind("<Return>", lambda _event: self.start_from_url())
        self.url_entry.bind("<FocusOut>", lambda _event: self.load_custom_for_url())
        ttk.Button(input_row, text="粘贴网址", command=self.paste_url).pack(side="left", padx=(0, 6))
        ttk.Button(input_row, text="▶ 开始自动填写", command=self.start_from_url).pack(side="left")
        self.url_entry.focus_set()

        profile = ttk.LabelFrame(root, text="我的常用资料（只存于本机 config.json）", padding=9)
        profile.pack(fill="x", padx=14, pady=5)
        for index, name in enumerate(PROFILE_FIELDS):
            r, group = divmod(index, 2)
            c = group * 2
            ttk.Label(profile, text=name + "：", width=9).grid(
                row=r, column=c, sticky="e", padx=(3, 0), pady=3
            )
            choice_fields = {
                "性别": ("", "男", "女"),
                "政治面貌": (
                    "", "中共党员", "中共预备党员", "共青团员", "群众",
                    "民主党派人士", "无党派人士", "少先队员", "其他",
                ),
                "是否为楼栋长、楼层长": ("", "是", "否"),
            }
            if name in choice_fields:
                ttk.Combobox(
                    profile, textvariable=self.profile_vars[name],
                    values=choice_fields[name], state="readonly", width=26,
                ).grid(row=r, column=c + 1, sticky="ew", padx=(0, 15), pady=3)
            else:
                ttk.Entry(profile, textvariable=self.profile_vars[name], width=29).grid(
                    row=r, column=c + 1, sticky="ew", padx=(0, 15), pady=3
                )
        profile.columnconfigure(1, weight=1)
        profile.columnconfigure(3, weight=1)

        extras = ttk.LabelFrame(root, text="本问卷的特殊答案（可选）", padding=7)
        extras.pack(fill="x", padx=14, pady=(3, 4))
        ttk.Label(
            extras,
            text="这里填写其他特殊题目答案，每行“题目=答案”。活动时间会默认选第一个可用选项，无需再手动填写。",
            wraplength=845,
        ).pack(anchor="w")
        self.custom_text = tk.Text(extras, height=3, font=("Microsoft YaHei", 10), wrap="word")
        self.custom_text.pack(fill="x", pady=(4, 1))

        controls = ttk.Frame(root, padding=(14, 2))
        controls.pack(fill="x")
        ttk.Checkbutton(
            controls, text="自动提交表单（首次测试勿勾选）", variable=self.auto_var,
        ).pack(side="left")
        ttk.Checkbutton(
            controls, text="自动选最后两道单选题（活动时间＋进群图片）",
            variable=self.join_var,
        ).pack(side="left", padx=(12, 0))
        ttk.Button(controls, text="保存资料和设置", command=self.save).pack(side="right", padx=3)
        ttk.Button(controls, text="重新读取设置", command=self.reload).pack(side="right", padx=3)

        options = ttk.Frame(root, padding=(14, 3))
        options.pack(fill="x")
        ttk.Button(options, text="打开图片文件夹", command=lambda: self.open_path(PICTURE_DIR)).pack(side="left", padx=3)
        ttk.Button(options, text="编辑专属题目答案", command=lambda: self.open_path(CONFIG_PATH)).pack(side="left", padx=3)
        ttk.Button(options, text="查看识别到的题目", command=lambda: self.open_path(FIELD_DIR)).pack(side="left", padx=3)

        action = ttk.Frame(root, padding=(14, 6))
        action.pack(fill="x")
        ttk.Label(action, text="待提交：").pack(side="left")
        self.review_combo = ttk.Combobox(
            action, textvariable=self.review_var, state="readonly", width=40, values=()
        )
        self.review_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.confirm_btn = ttk.Button(
            action, text="✓ 核对并提交所选问卷", command=self.confirm, state="disabled"
        )
        self.confirm_btn.pack(side="left", padx=(0, 8))
        self.skip_btn = ttk.Button(
            action, text="跳过所选问卷", command=self.cancel_current, state="disabled"
        )
        self.skip_btn.pack(side="left")
        self.status = tk.StringVar(value="就绪：每次点击开始都会新开一个标签页，无需等上一张问卷提交。")
        ttk.Label(root, textvariable=self.status, wraplength=870).pack(anchor="w", padx=17)

        logs = ttk.LabelFrame(root, text="运行记录", padding=7)
        logs.pack(fill="both", expand=True, padx=14, pady=(5, 12))
        self.logbox = ScrolledText(logs, height=8, state="disabled", wrap="word", font=("Consolas", 10))
        self.logbox.pack(fill="both", expand=True)

        self.append_log("已就绪：支持任意 HTTP(S) 问卷地址，点击“开始自动填写”即可测试。")
        self.append_log("首次使用请先双击“首次安装.bat”，下载 Playwright 浏览器组件。")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.worker = threading.Thread(target=self.browser_worker, daemon=True)
        self.worker.start()
        self.root.after(150, self.drain_events)

    def paste_url(self):
        try:
            content = self.root.clipboard_get().strip()
            if not content:
                raise ValueError("剪贴板为空")
            self.url_var.set(content)
            self.load_custom_for_url()
            self.append_log("网址已粘贴到输入框，请点击“开始自动填写”。")
        except (tk.TclError, ValueError):
            messagebox.showwarning("无法粘贴", "剪贴板没有可读取的文本，请直接粘贴到输入框。")

    def load_custom_for_url(self):
        urls = parse_urls(self.url_var.get())
        first = urls[0][1] if urls else None
        if first == self.active_custom_url:
            return
        self.active_custom_url = first
        self.custom_text.delete("1.0", "end")
        if first:
            values = self.config.get("每张问卷额外答案", {}).get(first, {})
            self.custom_text.insert(
                "1.0", "\n".join(f"{k}={v}" for k, v in values.items())
            )

    def start_from_url(self):
        self.load_custom_for_url()
        urls = parse_urls(self.url_var.get())
        if not urls:
            messagebox.showwarning(
                "网址有误", "请输入以 https:// 或 http:// 开头的有效网址，例如：https://example.com/form"
            )
            return
        if not self.save(quiet=True):
            return
        for form_id, url in urls:
            self.tasks.put(("open", form_id, url))
            self.append_log(f"已加入打开队列：{url}（将新开标签页）")
        self.status.set(f"已添加 {len(urls)} 个链接，正在等待浏览器处理。")

    def append_log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"[{ts}] {text}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def tell(self, text: str) -> None:
        self.ui_events.put(("log", text))

    def _refresh_review_controls(self, prefer_newest=False):
        values = list(self.review_items.keys())
        self.review_combo.configure(values=values)
        if not values:
            self.review_var.set("")
        elif prefer_newest or self.review_var.get() not in self.review_items:
            self.review_var.set(values[-1])
        state = "normal" if values else "disabled"
        self.confirm_btn.configure(state=state)
        self.skip_btn.configure(state=state)

    def drain_events(self):
        if self.closed:
            return
        try:
            while True:
                kind, content = self.ui_events.get_nowait()
                if kind == "log":
                    self.append_log(content)
                elif kind == "review_add":
                    job_id, title, url = content
                    caption = f"#{job_id} {title} [{urlsplit(url).hostname}]"
                    self.review_items[caption] = job_id
                    self._refresh_review_controls(prefer_newest=True)
                    self.status.set(f"已填完《{title}》，可以继续输入其他网址，或选择待提交问卷进行确认。")
                elif kind == "review_remove":
                    job_id, message = content
                    for key, value in list(self.review_items.items()):
                        if value == job_id:
                            del self.review_items[key]
                    self._refresh_review_controls()
                    self.status.set(message)
                elif kind == "working":
                    self.status.set(content)
                elif kind == "done":
                    self.status.set(content)
                    self._refresh_review_controls()
        except queue.Empty:
            pass
        self.root.after(150, self.drain_events)

    def save(self, quiet=False):
        try:
            cfg = read_config()
            cfg["个人资料"] = {
                key: self.profile_vars[key].get().strip() for key in PROFILE_FIELDS
            }
            cfg["自动提交"] = self.auto_var.get()
            cfg["自动确认最后进群选项"] = self.join_var.get()
            allowed = {
                "性别": {"", "男", "女"},
                "政治面貌": {
                    "", "中共党员", "中共预备党员", "共青团员", "群众",
                    "民主党派人士", "民主党派", "无党派人士", "少先队员", "其他",
                },
                "是否为楼栋长、楼层长": {"", "是", "否"},
            }
            for field, possible in allowed.items():
                if cfg["个人资料"].get(field, "") not in possible:
                    raise ValueError(f"{field}：请选择下拉框中的有效选项。")
            urls = parse_urls(self.url_var.get())
            if urls:
                raw = self.custom_text.get("1.0", "end").strip()
                extras = {}
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    sep = "=" if "=" in line else ("：" if "：" in line else None)
                    if not sep:
                        raise ValueError(f"特殊答案必须使用“题目=答案”格式：{line[:35]}")
                    key, value = (x.strip() for x in line.split(sep, 1))
                    if not key or not value:
                        raise ValueError(f"特殊答案的题目和内容不能留空：{line[:35]}")
                    extras[key] = value
                if extras:
                    cfg.setdefault("每张问卷额外答案", {})[urls[0][1]] = extras
                else:
                    cfg.setdefault("每张问卷额外答案", {}).pop(urls[0][1], None)
            CONFIG_PATH.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.config = cfg
            if not quiet:
                self.append_log("个人资料与提交设置已保存。")
                self.status.set("就绪：输入网址并点击“开始自动填写”。")
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return False

    def reload(self):
        try:
            self.config = read_config()
            for key in PROFILE_FIELDS:
                self.profile_vars[key].set(str(self.config["个人资料"].get(key, "")))
            self.auto_var.set(self.config["自动提交"])
            self.join_var.set(self.config["自动确认最后进群选项"])
            self.active_custom_url = None
            self.load_custom_for_url()
            self.append_log("已重新读取 config.json。")
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))

    @staticmethod
    def open_path(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == "":
            path.mkdir(exist_ok=True)
        import os
        import platform
        import subprocess
        if platform.system() == "Windows":
            os.startfile(str(path))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def confirm(self):
        job_id = self.review_items.get(self.review_var.get())
        if job_id is None:
            messagebox.showinfo("没有待提交问卷", "请先输入一个问卷网址并点击开始自动填写。")
            return
        self.tasks.put(("submit", job_id))
        self.confirm_btn.configure(state="disabled")
        self.append_log(f"已将第 {job_id} 张问卷加入提交任务，将在浏览器内确认提交结果。")

    def cancel_current(self):
        job_id = self.review_items.get(self.review_var.get())
        if job_id is None:
            return
        self.tasks.put(("skip", job_id))
        self.skip_btn.configure(state="disabled")
        self.append_log(f"已请求跳过第 {job_id} 张问卷（浏览器标签页会保留）。")

    def browser_worker(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.tell(f"未安装 Playwright：{exc}。请运行“首次安装.bat”。")
            return

        context = None
        sessions = {}
        job_number = 0

        def open_browser(p):
            BROWSER_DIR.mkdir(exist_ok=True)
            options = dict(
                user_data_dir=str(BROWSER_DIR),
                headless=False,
                accept_downloads=True,
                viewport={"width": 1260, "height": 880},
            )
            # 可选：开发测试可指定系统 Chromium 路径；正常使用自动下载的版本。
            import os
            override = os.environ.get("FORM_ASSISTANT_BROWSER_PATH", "").strip()
            if override:
                options["executable_path"] = override
            return p.chromium.launch_persistent_context(**options)

        def count_fields(frame):
            try:
                return frame.locator(
                    "input, textarea, select, [role='radio'], [role='checkbox']"
                ).count()
            except Exception:
                return 0

        def drop_session(job_id, message):
            sessions.pop(job_id, None)
            self.ui_events.put(("review_remove", (job_id, message)))

        try:
            with sync_playwright() as p:
                while not self.closed:
                    try:
                        command = self.tasks.get(timeout=0.35)
                    except queue.Empty:
                        continue

                    action = command[0]
                    if action == "skip":
                        _, job_id = command
                        drop_session(job_id, f"已跳过第 {job_id} 张问卷，浏览器标签页仍然保留。")
                        continue

                    if action == "submit":
                        _, job_id = command
                        session = sessions.get(job_id)
                        if session is None:
                            self.tell(f"第 {job_id} 张问卷已不在待提交列表。")
                            drop_session(job_id, "该问卷不在待提交列表。")
                            continue
                        title = session["title"]
                        try:
                            page = session["page"]
                            if page.is_closed():
                                drop_session(job_id, f"《{title}》标签页已关闭。")
                                continue
                            page.bring_to_front()
                            frame = session["frame"]
                            result = submit_form(frame)
                            if result:
                                self.tell(f"《{title}》检测到提交成功。")
                                drop_session(job_id, f"已提交《{title}》，并识别到成功提示。")
                            else:
                                self.tell(
                                    f"已点击《{title}》的提交按钮，但未识别到明确成功提示。"
                                    "请检查浏览器，避免重复提交。"
                                )
                                drop_session(job_id, f"《{title}》已尝试提交，结果需人工核对。")
                        except Exception as exc:
                            self.tell(f"《{title}》提交失败：{exc}")
                            self.ui_events.put(("done", f"提交未成功：{title}，请核对页面。"))
                        continue

                    if action != "open":
                        continue
                    _, form_id, url = command
                    job_number += 1
                    job_id = job_number
                    title = FORMS.get(form_id, urlsplit(url).hostname or form_id)
                    self.ui_events.put(("working", f"正在新标签页打开第 {job_id} 张问卷：{title}"))

                    try:
                        if context is None:
                            context = open_browser(p)
                        try:
                            page = context.new_page()  # 每次打开新标签页，旧页绝不覆盖
                        except Exception:
                            context = open_browser(p)
                            page = context.new_page()
                        page.bring_to_front()
                        self.tell(f"新标签页 #{job_id} 正在打开：{url}")
                        page.goto(url, wait_until="domcontentloaded", timeout=35000)
                        page.wait_for_timeout(1100)
                        try:
                            actual_title = page.locator("h1,h2").first.inner_text(timeout=1300).strip()
                            if 3 <= len(actual_title) <= 70:
                                title = actual_title
                        except Exception:
                            pass

                        # 协作者邀请链接不是填写链接，需要登录的页面也不能擅自处理。
                        if "/form_invites/" in urlsplit(url).path:
                            self.tell(
                                "这个网址是金数据协作邀请，不是填表页。"
                                "已在浏览器中打开，但不会自动接受邀请或提交数据。"
                            )
                            self.ui_events.put(("done", "已打开协作邀请，请使用表单发布后的填写网址测试填报。"))
                            continue

                        try:
                            page.locator(
                                "input,textarea,select,[role='radio'],[role='checkbox']"
                            ).first.wait_for(state="attached", timeout=5000)
                        except Exception:
                            pass

                        form_target = max(page.frames, key=count_fields, default=page.main_frame)
                        control_count = count_fields(form_target)
                        self.tell(
                            f"第 {job_id} 张《{title}》识别到 {control_count} 个输入控件，"
                            f"页面框架数 {len(page.frames)}。"
                        )
                        if control_count == 0:
                            self.tell(
                                "该页面没有可识别的问卷控件；"
                                "可能是登录、邀请或网页尚未加载完成，请在打开的标签页中查看。"
                            )
                            self.ui_events.put(("done", f"已打开《{title}》，但没有发现可填写字段。"))
                            continue

                        cfg = read_config()
                        self.tell(
                            "固定选择检查："
                            + "、".join(
                                f"{name}={'已设置' if str(cfg.get('个人资料', {}).get(name, '')).strip() else '未设置'}"
                                for name in ("性别", "政治面貌")
                            )
                        )
                        this_form_answers = [
                            cfg.get("每张问卷额外答案", {}).get(url, {}),
                            cfg.get("每张问卷额外答案", {}).get(form_id, {}),
                        ]
                        has_values = any(
                            str(v).strip() for v in cfg.get("个人资料", {}).values()
                        ) or any(
                            str(v).strip()
                            for d in this_form_answers for v in d.values()
                        )
                        if not has_values:
                            self.tell("本机没有保存任何答案，请先填写个人资料并点击保存。")

                        try:
                            qfile = save_question_list(form_target, form_id)
                            self.tell(f"题目清单已保存：{qfile.name}")
                        except Exception as exc:
                            self.tell(f"题目读取有问题：{exc}")

                        count, pending, reports = fill_form(form_target, form_id, cfg, url)

                        # 性别/政治面貌可能是隐藏的 input 或 div 自定义单选控件。
                        # 额外按页面上可见的题目标题查找，逐一回读确认选中状态。
                        fixed_count, fixed_results = fix_gender_and_political(
                            form_target, cfg, self.tell, form_id
                        )
                        count += fixed_count
                        for result in fixed_results:
                            if result.get("匹配结果") == "已选中":
                                filled_markers = [normalize(result["题目"])] + [
                                    normalize(name) for name in
                                    result.get("控件名称", [])
                                ]
                                pending = [
                                    item for item in pending
                                    if not any(marker and (
                                        normalize(item) == marker or
                                        marker == normalize(result["题目"]) and
                                        marker in normalize(item)
                                    ) for marker in filled_markers)
                                ]
                        reports.extend(fixed_results)

                        # 直接按问卷顺序处理最后两个单选题，不再猜“进群”按钮文案。
                        # 即：倒数第二组活动时间（若未选过则取第一个）、
                        # 最后一组带进群图片的题目（若未选过则取第一个）。
                        if cfg.get("自动确认最后进群选项", True):
                            last_two = auto_select_last_two_radio_groups(
                                form_target, self.tell, form_id
                            )
                            count += int(last_two["time_selected"])
                            count += int(last_two["group_selected"])
                            if last_two["time_selected"]:
                                pending = [item for item in pending if not any(
                                    k in normalize(item) for k in
                                    ("时间", "时段", "志愿时", "报名时")
                                )]
                            if last_two["group_selected"]:
                                pending = [item for item in pending if not any(
                                    k in normalize(item) for k in JOIN_WORDS
                                )]

                        # 先选中末题再保存截图；图片可能在选择后才显示。
                        capture_last_question_images(form_target, form_id, title, self.tell)

                        FIELD_DIR.mkdir(exist_ok=True)
                        diag_path = FIELD_DIR / f"{form_id}_填写诊断.json"
                        diag_path.write_text(
                            json.dumps(reports, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        self.tell(f"第 {job_id} 张《{title}》实际填入并校验了 {count} 项。")
                        if count == 0:
                            self.tell(f"没有任何字段成功填入，请查看 {diag_path.name}。")
                        if pending:
                            self.tell("待补充的题目：" + "、".join(pending[:9]))

                        required = missing_required(form_target)
                        password_count = form_target.locator("input[type='password']").count()
                        auto_ok = (
                            bool(cfg["自动提交"]) and count > 0 and not pending
                            and not required and not password_count
                        )
                        if cfg["自动提交"] and not auto_ok:
                            self.tell(
                                "未通过自动提交检查（存在未匹配题目、未填必填项等），"
                                "已保留在待提交列表供人工核对。"
                            )
                        if auto_ok:
                            self.tell(f"第 {job_id} 张已启用自动提交。")
                            try:
                                success = submit_form(form_target)
                                if success:
                                    self.ui_events.put(("done", f"《{title}》提交成功。"))
                                    continue
                                self.tell("已点提交但未确认成功，请检查浏览器里的提示。")
                            except Exception as exc:
                                self.tell(f"自动提交失败，保留人工提交入口：{exc}")

                        sessions[job_id] = {
                            "page": page, "frame": form_target,
                            "title": title, "url": url,
                        }
                        self.ui_events.put(("review_add", (job_id, title, url)))
                        self.tell(
                            f"第 {job_id} 张可以继续留在浏览器标签页中核对。"
                            "无需先提交它，直接输入下一个网址并点“开始自动填写”即可。"
                        )
                    except Exception as exc:
                        self.tell(f"打开或填写第 {job_id} 张问卷失败：{exc}")
                        self.ui_events.put(("done", f"打开失败：{title}，可再次点击开始重试。"))
                        try:
                            FIELD_DIR.mkdir(exist_ok=True)
                            (FIELD_DIR / "错误日志.txt").write_text(
                                traceback.format_exc(), encoding="utf-8"
                            )
                        except Exception:
                            pass
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
        except Exception as exc:
            self.tell(f"浏览器工作线程异常：{exc}。请检查依赖环境。")
            self.ui_events.put(("done", "浏览器工作线程出错，请重启助手。"))

    def close(self):
        self.closed = True
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
