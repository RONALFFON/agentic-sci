"""论文分析智能体 —— PDF 全文解析、表格提取与深度问答."""

from __future__ import annotations

import logging
import copy
import json
from pathlib import Path
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from hello_agents import SimpleAgent
from hello_agents.tools.registry import ToolRegistry

from app.core.paper_paths import normalize_library_category_display

from ..utils import parse_llm_json
from ..services.llm.agent_config import papergraph_agent_config
from ..services.llm.agent_runtime import check_active, _run_with_optional_timeout, _task_failed_due_to_timeout
from ..services.task_state import identity, file_version, task_lock
from ..services.llm.llm_service import is_llm_configured, coerce_hello_agents_llm_output_to_str
from .base import BaseAgent
from .support.paper_analysis_helpers import (
    build_reader_prompt,
    clip_text as _clip,
    clip_reader_history as _clip_reader_history,
    clean_library_tag as _clean_library_tag,
    dedupe_tags as _dedupe_tags,
    nearest_major_in as _nearest_major_in,
    parse_taxonomy_majors as _parse_taxonomy_majors,
    prioritize_reader_context as _prioritize_reader_context,
    top_similar_categories as _top_similar_categories,
)
from .support.reader_pdf_parse_tool import ReaderPdfParseTool
from .support.reader_table_tool import ReaderTableTool
from .support.reader_paper_lookup_tool import ReaderPaperLookupTool, ground_score_paper_vs_reference_blob
from .support.reader_reference_lookup_tool import (
    READER_RECOMMEND_MAX_RESULTS, ReaderReferenceLookupTool,
    READER_RELATED_FROM_BIBLIOGRAPHY, READER_RELATED_FROM_REF_BLOCK,
    paper_matches_reader_snap, parse_reader_recommendation_intent,
    prioritize_reader_related_pairs_refs_first, reader_user_allows_external_paper_lookup,
    rerank_reader_pairs_by_anchor_refs_first, resolve_references_via_openalex,
    strip_reader_reco_boilerplate, user_message_may_need_reference_lookup,
    READER_RELATED_FROM_PRE_SEARCH,
)

from .prompts.paper_analysis import ANALYSIS_SYSTEM, READER_CHAT_SYSTEM

logger = logging.getLogger(__name__)

def _reader_resolve_user_hint(user_message: str, snap: Dict[str, Any], *, want_reco: bool) -> str:
    um = (user_message or "").strip()
    if not want_reco:
        return um[:900]
    core = strip_reader_reco_boilerplate(um) or um
    if len(core) >= 28:
        return um[:900]
    title = str(snap.get("title") or "").strip()
    ab = str(snap.get("abstract") or "").strip()[:520]
    bits = [x for x in (core, title, ab) if x]
    return "\n".join(bits).strip()[:900] or um[:900]

_MAJOR_LOCK = threading.Lock()
_MAJOR_WHITELISTS: dict[str, Tuple[str, ...]] = {}

@dataclass
class TaskSpec:
    name: str
    agent: Any
    parser: Optional[Callable[[str], Any]] = None
    max_chars: int = 6000

class PaperAnalysisAgent(BaseAgent):
    """论文分析核心智能体 —— 管理多个子 Agent 协同完成 PDF 解析、表格提取与问答."""

    def __init__(self) -> None:
        super().__init__()
    @property
    def _analysis(self):
        return SimpleAgent(name="papergraph_analysis", llm=self.llm, system_prompt=ANALYSIS_SYSTEM, config=papergraph_agent_config())

    def _init_reader_execution(self) -> None:
        self._reader_lookup_lock = threading.Lock()
        self._reader_lookup_buffer: List[Tuple[Any, str]] = []
        self._reader_snap: Dict[str, Any] = {}
        self._reader_last_user_message: str = ""

        self._reader_reco_ref_offset: Dict[int, int] = {}
        _reader_reg = ToolRegistry()
        _reader_reg.register_tool(
            ReaderPaperLookupTool(
                on_papers_found=self._reader_tool_on_found,
                get_snap=lambda: getattr(self, "_reader_snap", None) or {},
            )
        )
        _reader_reg.register_tool(
            ReaderReferenceLookupTool(
                get_snap=lambda: getattr(self, "_reader_snap", None) or {},
                on_papers_found=self._reader_tool_on_found,
                get_user_message=lambda: getattr(self, "_reader_last_user_message", "") or "",
            )
        )
        _reader_reg.register_tool(
            ReaderPdfParseTool(
                get_snap=lambda: getattr(self, "_reader_snap", None) or {},
                on_parsed=self._reader_on_pdf_structure,
            )
        )
        _reader_reg.register_tool(
            ReaderTableTool(
                get_snap=lambda: getattr(self, "_reader_snap", None) or {},
            )
        )
        self._reader = SimpleAgent(
            name="papergraph_paper_reader",
            llm=self.llm,
            system_prompt=READER_CHAT_SYSTEM,
            config=papergraph_agent_config(),
            tool_registry=_reader_reg,
            enable_tool_calling=True,
            max_tool_iterations=5,
        )

    def _ensure_major_whitelist(self) -> None:
        root = str(Path(self._settings.data_dir).resolve())
        taxonomy_path = Path(root) / "memory" / "taxonomy-v1.json"
        if root in _MAJOR_WHITELISTS:
            return
        with _MAJOR_LOCK:
            if root in _MAJOR_WHITELISTS:
                return
            try:
                if taxonomy_path.is_file():
                    saved = _parse_taxonomy_majors(taxonomy_path.read_text(encoding="utf-8"))
                    if saved:
                        _MAJOR_WHITELISTS[root] = tuple(saved)
                        return
            except (OSError, ValueError):
                logger.warning("major_taxonomy_cache_invalid")
            wl: Optional[Tuple[str, ...]] = None
            taxonomy_prompt = (
                "# 任务：为个人/小团队文献库生成顶层大类\n"
                '输出: {"majors":["名称1",...]}\n'
                "- 共 16～24 条；恰含一个「未分类」\n"
                "- 名称须具学术划分意义，覆盖计算机与交叉学科\n"
                "- 每条 2～10 个中文字；互异；禁含「/」及路径非法字符\n"
            )
            try:
                raw = _run_with_optional_timeout(lambda: self._analysis.run(taxonomy_prompt), 30)
                parsed = _parse_taxonomy_majors(raw)
                if not parsed:
                    raw = _run_with_optional_timeout(lambda: self._analysis.run(taxonomy_prompt + '\n请确保输出合法 JSON。'), 30)
                    parsed = _parse_taxonomy_majors(raw)
                if parsed:
                    wl = tuple(parsed)
            except Exception:
                logger.exception("major_taxonomy_bootstrap_failed")
            if not wl:
                raise RuntimeError("major_taxonomy_bootstrap_failed")
            check_active()
            taxonomy_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = taxonomy_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"majors": list(wl)}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(taxonomy_path)
            _MAJOR_WHITELISTS[root] = wl
            logger.info("paper_analysis_major_whitelist_ready", extra={"n": len(wl)})

    def _get_major_whitelist(self) -> Tuple[str, ...]:
        self._ensure_major_whitelist()
        return _MAJOR_WHITELISTS[str(Path(self._settings.data_dir).resolve())]

    def _cleanup_mixed_reader_response(self, reply: str, user_message: str) -> str:
        """Use one cleanup pass when tool output leaks into the final reply."""
        t = reply.strip()
        # 只有工具包装泄漏才追加整理调用，正常的证据不足说明不触发。
        has_tool_artifact = "reader_pdf_struct" in t or '"chapters"' in t[:500]

        if has_tool_artifact:
            if not is_llm_configured():
                return reply
            logger.info("paper_reader: detected mixed response, running cleanup pass")
            try:
                um = (user_message or "").strip()
                # Keep extracted paper facts; drop wrapper noise.
                cleaned = re.sub(r"^.*?(?:当前文献材料说明|当前提供的材料).*?\n\n", "", t, flags=re.S)
                cleaned = re.sub(r"reader_pdf_struct\S*", "", cleaned)
                cleaned = cleaned.strip()[:6000]
                if cleaned:
                    prompt = (
                        f"用户问：{um}\n\n"
                        f"以下是从论文中提取的信息（可能含多个片段）：\n\n{cleaned}\n\n"
                        "请整合成一个连贯的回答。用中文分点说明。如有表格数据用 Markdown 表格呈现。"
                        "保留原回答的证据边界和不确定性；只基于已有信息回答，缺失信息标注'论文未提供'。"
                    )
                    result = self._reader_chat_llm(prompt)
                    if result.strip():
                        return result.strip()
            except Exception:
                logger.debug("cleanup_mixed_response_failed", exc_info=True)
        return reply

    @staticmethod
    def _looks_like_raw_tool_output(text: str) -> bool:
        """Detect raw tool output that still needs interpretation."""
        t = (text or "").strip()
        if not t:
            return False
        if "reader_pdf_structure" in t:
            return True
        if '"chapters"' in t[:500] or '"references"' in t[:500]:
            return True
        return False

    def _interpret_tool_output(self, tool_output: str, user_message: str) -> str:
        """Convert raw tool output into a user-facing answer."""
        if not is_llm_configured():
            return tool_output
        try:
            um = (user_message or "").strip()
            # Strip tool wrappers before asking the LLM to explain.
            cleaned = re.sub(r"^.*?reader_pdf_structure[：:]\s*", "", tool_output, flags=re.S)
            cleaned = re.sub(r"^以下为 JSON.*?\n", "", cleaned)
            cleaned = re.sub(r"^\s*\{\s*\"chapters\".*?\n", "", cleaned)
            cleaned = re.sub(r"reader_pdf_structu\S*$", "", cleaned)
            cleaned = re.sub(r"[\u007F-\u009F]", "", cleaned)
            cleaned = cleaned.strip()
            if not cleaned or len(cleaned) < 50:
                return "当前文献材料不足以回答该问题。PDF 文本提取不完整，建议确认 PDF 文件是否可读。"
            cleaned = cleaned[:6000]
            prompt = (
                f"用户问：{um}\n\n"
                f"以下是从论文中提取的相关章节内容：\n\n{cleaned}\n\n"
                "请用中文为用户解读这段内容。分点说明关键发现、方法和结论。"
                "用 Markdown 表格对比数据（如有）。\n"
                "重要：如果上面的内容不足以回答用户问题（如仅有章节标题无正文），"
                "请直接说明材料不足，严禁编造论文中不存在的数据、方法或结论。"
            )
            raw = self._reader_chat_llm(prompt)
            return raw.strip() or tool_output
        except Exception:
            logger.debug("interpret_tool_output_failed", exc_info=True)
            return tool_output

    def _reader_chat_llm(self, prompt: str) -> str:
        from ..services.llm.agent_runtime import run_agent_task
        return run_agent_task(task_name="reader_interpreter", agent_name="reader_interpreter", llm=self.llm,
                              system_prompt=READER_CHAT_SYSTEM, user_prompt=prompt, retries=0)

    def _reader_tool_on_found(self, papers: List[Any], source: str) -> None:
        with self._reader_lookup_lock:
            for p in papers or []:
                self._reader_lookup_buffer.append((p, source))

    def _reader_on_pdf_structure(self, obj: Dict[str, Any]) -> None:
        try:
            snap = getattr(self, "_reader_snap", None)
            if not isinstance(snap, dict):
                return
            refs = obj.get("references") or {}
            entries = refs.get("entries")
            if isinstance(entries, list) and entries:
                snap["references_from_structure"] = [str(x).strip() for x in entries if str(x).strip()]
        except Exception:
            logger.debug("reader_on_pdf_structure_failed", exc_info=True)

    @staticmethod
    def _dedupe_reader_paper_pairs(buffer: List[Tuple[Any, str]]) -> List[Tuple[Any, str]]:
        seen: set[str] = set()
        out: List[Tuple[Any, str]] = []
        for p, src in buffer:
            title = str(getattr(p, "title", "") or "").strip().casefold()
            if not title:
                continue
            keys = {"title:" + title}
            doi = str(getattr(p, "doi", "") or "").strip().casefold().removeprefix("https://doi.org/")
            arxiv = re.sub(r"v[0-9]+$", "", str(getattr(p, "arxiv_id", "") or "").strip().casefold())
            if doi:
                keys.add("doi:" + doi)
            if arxiv:
                keys.add("arxiv:" + arxiv)
            if keys & seen:
                continue
            seen.update(keys)
            out.append((p, src))
        return out
    def _run_task(self, spec: TaskSpec, user: str) -> Any:
        prompt = _clip(user, spec.max_chars)
        try:

            raw = _run_with_optional_timeout(lambda: spec.agent.run(prompt), 60)
        except Exception as exc:
            if _task_failed_due_to_timeout(exc):
                raise TimeoutError(f"paper_analysis_timeout:{spec.name}") from exc
            logger.exception("paper_analysis_llm_failed", extra={"task": spec.name})
            raise RuntimeError(f"paper_analysis_llm_failed:{spec.name}") from exc

        raw_text = (raw if isinstance(raw, str) else str(raw or "")).strip()

        if not spec.parser:
            if not raw_text:
                if spec.name == "paper_reader_reply":
                    return ""
                raise RuntimeError(f"paper_analysis_empty_response:{spec.name}")

            # Some tool responses need a final explanation pass.
            if spec.name == "paper_reader_reply" and self._looks_like_raw_tool_output(raw_text):
                logger.info("paper_reader: detected raw tool output, invoking interpreter")
                raw_text = self._interpret_tool_output(raw_text, self._reader_last_user_message)

            return raw_text

        data = spec.parser(raw_text)
        if data is not None:
            return data

        try:
            raw2 = _run_with_optional_timeout(lambda: self._analysis.run("请只输出合法 JSON。\n" + prompt), 30)
            data2 = spec.parser((raw2 or "").strip())
            if data2 is not None:
                return data2
        except Exception:
            logger.exception("paper_analysis_json_retry_failed", extra={"task": spec.name})
        raise RuntimeError(f"paper_analysis_parse_failed:{spec.name}")

    def _record_preference_signals(
        self,
        *,
        signal: str,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
        major: Optional[str] = None,
        shared: bool = True,
    ) -> None:
        try:
            from ..services.memory.agent_memory import get_agent_memory

            am = get_agent_memory()
            parts: List[str] = [f"偏好信号({signal})"]
            if title:
                parts.append(f"title={str(title).strip()[:120]}")
            if major:
                parts.append(f"major={str(major).strip()[:24]}")
            if category:
                parts.append(f"cat={str(category).strip()[:40]}")
            if tags:
                clean = [str(x).strip() for x in (tags or []) if str(x).strip()][:10]
                if clean:
                    parts.append("tags=" + ",".join(clean))
            line = " | ".join(parts)[:360]
            am.add(agent_name="paper_analysis", content=line, memory_type="preference", importance=0.55, shared=bool(shared))
        except Exception:
            return

    def _parse_major_json(self, raw: str) -> Optional[str]:
        d = parse_llm_json(raw)
        if not isinstance(d, dict):
            return None
        m = str(d.get("major") or d.get("category") or "").strip()
        if not m:
            return None
        return _nearest_major_in(m, self._get_major_whitelist())

    def _parse_fine_classify_json(self, raw: str, major: str) -> Optional[Tuple[str, List[str]]]:
        d = parse_llm_json(raw)
        if not isinstance(d, dict):
            return None
        cat = normalize_library_category_display(str(d.get("category") or "未分类"))
        tags_raw = d.get("tags")
        extra: List[str] = []
        if isinstance(tags_raw, list):
            for x in tags_raw:
                c = _clean_library_tag(str(x))
                if c:
                    extra.append(c)
            extra = _dedupe_tags(extra)
        if not cat:
            return None
        if major and major != "未分类" and not (cat.startswith(major) or major in cat):
            cat = normalize_library_category_display(f"{major}/{cat.split('/')[-1]}")
        return cat, extra

    _venue_type_cache: dict[str, str] = {}

    def classify_venue_type(self, journal: str | None) -> str | None:
        if not journal or not str(journal).strip():
            return None
        j = str(journal).strip()
        if j.startswith("arXiv:"):
            return "preprint"
        from ..services.task_state import identity
        key = identity("venue-v2", self._settings.data_dir, self.llm.model, getattr(self.llm, "base_url", ""), j)
        if key in self._venue_type_cache:
            return self._venue_type_cache[key]
        try:
            prompt = f'判断以下学术来源名称是会议(conference)还是期刊(journal)。只回复一个单词：conference 或 journal。\n\n名称：{j}'
            resp = _run_with_optional_timeout(lambda: self._analysis.run(prompt), 20)
            result = str(resp).strip().lower()
            if "conference" in result:
                vt = "conference"
            elif "journal" in result:
                vt = "journal"
            else:
                vt = None
        except Exception:
            vt = None
        if vt:
            check_active()
            if len(self._venue_type_cache) >= 256:
                self._venue_type_cache.pop(next(iter(self._venue_type_cache)), None)
            self._venue_type_cache[key] = vt
        return vt

    def classify_for_library(
        self,
        title: str,
        abstract: Optional[str],
        journal: Optional[str],
        keywords: Optional[List[str]] = None,
        existing_categories: Optional[List[str]] = None,
    ) -> Tuple[str, List[str]]:
        kw = "、".join(keywords or []) or "（无）"
        journal = journal or "（无）"
        abstract = (abstract or "").strip() or "（无摘要）"
        seed = f"{title}\n{abstract[:800]}"

        cats_all = [str(x).strip() for x in (existing_categories or []) if str(x).strip()]
        candidates = _top_similar_categories(seed, cats_all, k=18)

        base_user = f"标题：{title}\n摘要：{abstract}\n来源：{journal}\n关键词：{kw}"

        wl = self._get_major_whitelist()
        major_list_block = "\n".join(f"- {m}" for m in wl)
        major_user = f"{base_user}\n\n【可选大类列表】\n{major_list_block}"

        major_fb = "未分类"
        major_user = (
            "# 任务：归类（大类）\n"
            "从【可选大类列表】中选一个最匹配的大类\n"
            '输出: {"major":"大类名"}\n'
            '- 必须从列表选；优先字面匹配，否则语义最接近；无法判断→"未分类"；禁列表外值\n\n'
            + major_user
        )
        major_spec = TaskSpec(
            name="classify_major",
            agent=self._analysis,
            parser=self._parse_major_json,
            max_chars=5200,
        )
        major = self._run_task(major_spec, major_user)
        if not isinstance(major, str) or not major.strip():
            major = major_fb
        major = _nearest_major_in(major, wl)

        if not candidates:
            cat0 = normalize_library_category_display(major)
            self._record_preference_signals(signal="classify", title=title, major=major, category=cat0, shared=True)
            return cat0, []

        prefixed = [c for c in candidates if c.startswith(major) or c.split("/")[0] == major]
        pool = prefixed if len(prefixed) >= 2 else candidates
        pool_block = "\n".join(f"- {c}" for c in pool[:18])

        fine_user = (
            "# 任务：归类（路径与标签）\n"
            "给定大类，从候选已有路径中选择路径，生成标签\n"
            '输出: {"category":"路径","tags":["标签1",...]}\n'
            "- category：优先原样选候选；否则「大类／子类」，子类 2～8 个中文字\n"
            "- tags：3～8 个，单条 ≤24 字，不重复；无法判断可为 []\n"
            "- 禁含路径非法字符 \\ / : * ? \" < > |\n\n"
            f"{base_user}\n\n给定大类：{major}\n\n"
            f"【候选已有路径】（请优先从中复制一条作为 category）\n{pool_block}"
        )
        default_cat = normalize_library_category_display(major)

        def _fine_parser(raw: str) -> Optional[Tuple[str, List[str]]]:
            return self._parse_fine_classify_json(raw, major)

        fine_spec = TaskSpec(
            name="classify_fine",
            agent=self._analysis,
            parser=_fine_parser,
            max_chars=5500,
        )
        out = self._run_task(fine_spec, fine_user)
        if isinstance(out, tuple) and len(out) == 2:
            cat, tags = out[0], out[1]
            cat = normalize_library_category_display(str(cat or "未分类"))
            if isinstance(tags, list):
                cleaned: List[str] = []
                for x in tags:
                    c = _clean_library_tag(str(x))
                    if c:
                        cleaned.append(c)
                tags = _dedupe_tags(cleaned)
            else:
                tags = []
            self._record_preference_signals(signal="classify", title=title, major=major, category=cat, tags=tags, shared=True)
            return cat, tags
        return default_cat, []

    def paper_reader_reply(self, context_block: str, history_lines: str, user_message: str,
                           reader_snap: Optional[Dict[str, Any]] = None) -> Tuple[str, List[Any], List[str]]:
        execution = copy.copy(self)
        execution._init_reader_execution()
        return _run_with_optional_timeout(lambda: execution._paper_reader_reply(context_block, history_lines, user_message, reader_snap), 120)

    def _paper_reader_reply(
        self,
        context_block: str,
        history_lines: str,
        user_message: str,
        reader_snap: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, List[Any], List[str]]:
        snap: Dict[str, Any] = dict(reader_snap or {})
        self._reader_snap = snap
        reco_pid: Optional[int] = None
        prior_recommendations = sum(1 for line in history_lines.splitlines() if line.startswith("用户：") and parse_reader_recommendation_intent(line)[0])
        try:
            spid = snap.get("paper_id")
            if spid is not None and int(spid) > 0:
                reco_pid = int(spid)
        except (TypeError, ValueError):
            reco_pid = None
        try:
            with self._reader_lookup_lock:
                self._reader_lookup_buffer.clear()
            ctx = _prioritize_reader_context(context_block, max_chars=3600)
            hist = _clip_reader_history((history_lines or "").strip() or "（尚无此前对话）", max_chars=2200)
            um = str(user_message or "").strip()
            want_reco, reco_max = parse_reader_recommendation_intent(um)
            self._reader_last_user_message = um

            try:
                from ..services.memory.agent_memory import get_agent_memory

                mem_block = get_agent_memory(snap.get("_db_path")).build_context_block(agent_name="paper_analysis", query=um)
            except Exception:
                mem_block = ""
            user = build_reader_prompt(um, ctx, hist, mem_block, model=getattr(self.llm, "model", "gpt-4"),
                                       max_tokens=min(12000, self._settings.agent_context_window // 2))
            spec = TaskSpec(name="paper_reader_reply", agent=self._reader, parser=None, max_chars=len(user))
            out = self._run_task(spec, user)
            # Clean up mixed tool/user-facing output.
            if isinstance(out, str) and out.strip():
                out = self._cleanup_mixed_reader_response(out, um)
            with self._reader_lookup_lock:
                raw_pairs = list(self._reader_lookup_buffer)
                self._reader_lookup_buffer.clear()
            pairs = self._dedupe_reader_paper_pairs(raw_pairs)
            rb_pdf = str(snap.get("references_section_raw") or "").strip()

            if (
                len(rb_pdf) >= 140
                and not (snap.get("references") or [])
            ):
                _thr_bib = 0.48 if want_reco else 0.54
                pairs = [
                    (p, s) for p, s in pairs
                    if s != READER_RELATED_FROM_BIBLIOGRAPHY
                    or ground_score_paper_vs_reference_blob(p, rb_pdf) >= _thr_bib
                ]
            if user_message_may_need_reference_lookup(um) and len(pairs) == 0:
                try:
                    fb_max = reco_max if want_reco else 2
                    resolve_mr = max(fb_max, min(READER_RECOMMEND_MAX_RESULTS, fb_max * 4)) if want_reco else fb_max
                    extra: List[Any] = []
                    if snap.get("references"):
                        refs_full = [str(x).strip() for x in (snap.get("references") or []) if str(x).strip()]
                        off = self._reader_reco_ref_offset.get(reco_pid, prior_recommendations * 2) if reco_pid else 0
                        snap_res: Dict[str, Any] = snap
                        if want_reco and reco_pid and refs_full and off >= len(refs_full):
                            off = 0
                            self._reader_reco_ref_offset[reco_pid] = 0
                        if want_reco and reco_pid and off > 0 and off < len(refs_full):
                            snap_res = dict(snap)
                            snap_res["references"] = refs_full[off:]
                        extra = resolve_references_via_openalex(
                            snap_res,
                            max_results=resolve_mr,
                            user_hint=_reader_resolve_user_hint(um, snap, want_reco=want_reco),
                        )
                        if extra and want_reco and reco_pid:
                            self._reader_reco_ref_offset[reco_pid] = off + max(1, len(extra))
                    elif (snap.get("references_section_raw") or "").strip():
                        from ..services.reader.paper_reader_context import reference_strings_for_resolve_fallback

                        ref_lines = reference_strings_for_resolve_fallback(
                            str(snap.get("references_section_raw") or "")
                        )
                        rs_struct = snap.get("references_from_structure")
                        if isinstance(rs_struct, list) and rs_struct:
                            ref_lines = [str(x).strip() for x in rs_struct if str(x).strip()] or ref_lines
                        ref_core = list(ref_lines)
                        if ref_core and is_llm_configured():
                            try:
                                from ..services.reader.reader_recommend_llm import merge_ref_lines_with_llm_queries

                                merged = merge_ref_lines_with_llm_queries(
                                    str(snap.get("references_section_raw") or ""),
                                    snap,
                                    ref_core,
                                    max_queries=12,
                                )
                                ref_lines = merged if merged else ref_core
                            except Exception:
                                logger.debug("merge_llm_ref_queries_failed", exc_info=True)
                                ref_lines = ref_core
                        else:
                            ref_lines = ref_core
                        if ref_lines:
                            off = self._reader_reco_ref_offset.get(reco_pid, prior_recommendations * 2) if reco_pid else 0
                            if want_reco and reco_pid and off >= len(ref_lines):
                                off = 0
                                self._reader_reco_ref_offset[reco_pid] = 0
                            if want_reco and reco_pid and off > 0:
                                ref_lines = ref_lines[off:]
                            if ref_lines:
                                snap_fb = dict(snap)
                                snap_fb["references"] = ref_lines
                                extra = resolve_references_via_openalex(
                                    snap_fb,
                                    max_results=resolve_mr,
                                    user_hint=_reader_resolve_user_hint(um, snap, want_reco=want_reco),
                                )
                                rb = str(snap.get("references_section_raw") or "").strip()
                                if extra and len(rb) >= 140 and not (snap.get("references") or []):
                                    raw_extra = list(extra)

                                    def _gf(th: float) -> List[Any]:
                                        return [
                                            p
                                            for p in raw_extra
                                            if ground_score_paper_vs_reference_blob(p, rb) >= th
                                        ]

                                    extra = _gf(0.54)
                                    if not extra and want_reco:
                                        extra = _gf(0.42)
                                if extra and want_reco and reco_pid:
                                    self._reader_reco_ref_offset[reco_pid] = off + max(1, len(extra))
                    pairs.extend((paper, READER_RELATED_FROM_BIBLIOGRAPHY if snap.get("references") else READER_RELATED_FROM_REF_BLOCK) for paper in extra)
                except Exception:
                    logger.debug("reader_reference_server_fallback_failed", exc_info=True)

            bib_only = (
                (want_reco or user_message_may_need_reference_lookup(um))
                and not reader_user_allows_external_paper_lookup(um)
            )
            pairs = [(p, s) for p, s in self._dedupe_reader_paper_pairs(pairs) if not paper_matches_reader_snap(snap, p)]
            if bib_only:
                pairs = [
                    (p, s)
                    for p, s in pairs
                    if s in (READER_RELATED_FROM_BIBLIOGRAPHY, READER_RELATED_FROM_REF_BLOCK)
                ]
            if want_reco and pairs:
                try:
                    if is_llm_configured():
                        from ..services.reader.reader_recommend_llm import rerank_reader_recommend_pairs_by_llm

                        pairs = rerank_reader_recommend_pairs_by_llm(
                            snap,
                            pairs,
                            user_message=um,
                            history_lines=hist,
                            reco_max_hint=reco_max,
                        )
                    else:
                        pairs = rerank_reader_pairs_by_anchor_refs_first(snap, pairs, k=reco_max)
                except Exception:
                    logger.debug("reader_llm_recommend_rerank_failed", exc_info=True)
                    pairs = rerank_reader_pairs_by_anchor_refs_first(snap, pairs, k=reco_max)
            if pairs:
                pairs = prioritize_reader_related_pairs_refs_first(pairs)
            if want_reco and pairs:
                cap = max(1, min(int(reco_max or 2), READER_RECOMMEND_MAX_RESULTS, len(pairs)))
                pairs = pairs[:cap]
            papers = [p for p, _ in pairs]
            provenances = [s for _, s in pairs]
            text_out = (str(out) if out is not None else "").strip()
            if not text_out:
                if papers:
                    if snap.get("references_source") == "pdf_section":
                        text_out = (
                            "已根据 PDF 参考文献区摘录检索到相关论文，请点击下方「推荐论文」查看条目；"
                            "如需结合摘要或方法做对比，请告诉我关注点。"
                        )
                    else:
                        text_out = (
                            "已根据参考文献检索列出相关论文，请点击下方「推荐论文」查看条目；"
                            "如需结合摘要或方法做对比，请告诉我关注点。"
                        )
                elif user_message_may_need_reference_lookup(um) and not (snap.get("references") or []) and not (
                    snap.get("references_section_raw") or ""
                ).strip():
                    text_out = (
                        "未在库表中找到 references，且当前 PDF 摘录中未能定位到「参考文献 / References」标题后的文本块，"
                        "无法从原文区检索。若为扫描版、参考文献不在已抽取页范围内，或版式特殊，会出现此情况。"
                        "可从带参考文献的数据源重新保存该文，或直接粘贴英文题名 / DOI 以便检索。"
                    )
                elif user_message_may_need_reference_lookup(um) and (snap.get("references_section_raw") or "").strip():
                    text_out = (
                        "上下文中已含 PDF 参考文献区原文，但本轮未产生可展示的检索命中。"
                        "你可指定要查的一条英文题名或 DOI；或让我从摘录中逐条用检索工具核对。"
                    )
                elif user_message_may_need_reference_lookup(um):
                    text_out = (
                        "已按库表参考文献题录在 OpenAlex / arXiv / DBLP 中尝试解析，但未找到与题录足够一致的条目"
                        "（已过滤明显不符的综述/泛命中）。你可粘贴 DOI 或标准英文题名，我会用 reader_paper_lookup 检索并展示在下方列表。"
                    )
                else:
                    text_out = "（本次未收到模型有效正文。请稍后重试，或缩短问题后再次提问。）"

            if want_reco and papers:
                # 模型生成后还有去重、证据筛选和重排，重新建立最终卡片序号。
                text_out = re.sub(r"(?<!!)\[\d+(?:\s*[,，、-]\s*\d+)*\](?!\()", "", text_out)
                verified = "\n".join(f"[{i}] {getattr(paper, 'title', '')}" for i, paper in enumerate(papers, 1))
                text_out = text_out.rstrip() + "\n\n本轮核实的论文（与推荐卡片顺序一致）：\n" + verified
            check_active()
            return (text_out, papers, provenances)
        finally:
            self._reader_snap = {}
