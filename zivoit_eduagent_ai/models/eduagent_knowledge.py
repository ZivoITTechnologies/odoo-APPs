# -*- coding: utf-8 -*-
"""Course RAG store: chunks course content and FAQs, retrieves by embeddings or keywords."""
import json
import math
import re
from collections import Counter

from odoo import _, api, fields, models
from odoo.tools import html2plaintext

WORD_RE = re.compile(r"[\w']+", re.UNICODE)
STOPWORDS = set("""a an the and or of to in on for is are was were be been with as at by it this that
from what how why when which who can could would should do does did i you we they he she my your our
their me us them not no yes if then so than too very just about into over under also please tell explain
""".split())
CHUNK_SIZE = 900


def tokenize(text):
    return [w for w in WORD_RE.findall((text or "").lower()) if w not in STOPWORDS and len(w) > 2]


def split_chunks(text, size=CHUNK_SIZE):
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:
            chunks.append((cur + "\n" + p[:size]).strip())
            cur, p = "", p[size:]
        if len(cur) + len(p) > size and cur:
            chunks.append(cur.strip())
            cur = p
        else:
            cur = (cur + "\n\n" + p).strip()
    if cur:
        chunks.append(cur)
    return chunks


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)


class EduagentKnowledgeChunk(models.Model):
    _name = "eduagent.knowledge.chunk"
    _description = "EduAgent Knowledge Chunk (RAG store)"
    _order = "channel_id, slide_id, sequence"

    name = fields.Char(string="Title", required=True)
    sequence = fields.Integer()
    content = fields.Text(required=True)
    source_type = fields.Selection([
        ("slide", "Course Content"), ("faq", "FAQ / Policy"), ("course", "Course Catalog"),
        ("manual", "Manual")], required=True, default="manual")
    channel_id = fields.Many2one("slide.channel", string="Course", ondelete="cascade", index=True)
    slide_id = fields.Many2one("slide.slide", string="Lesson", ondelete="cascade")
    faq_id = fields.Many2one("eduagent.faq", ondelete="cascade")
    url = fields.Char()
    embedding = fields.Text(help="JSON-encoded embedding vector")
    keywords = fields.Text()
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "keywords" not in vals:
                vals["keywords"] = " ".join(tokenize((vals.get("name") or "") + " " + (vals.get("content") or "")))
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------
    @api.model
    def _index_records(self, vals_list):
        if not vals_list:
            return self.browse()
        chunks = self.sudo().create(vals_list)
        vectors = self.env["eduagent.llm"].embed([c.name + "\n" + c.content for c in chunks])
        if vectors and len(vectors) == len(chunks):
            for chunk, vec in zip(chunks, vectors):
                chunk.embedding = json.dumps(vec)
        return chunks

    @api.model
    def index_channel(self, channel):
        channel = channel.sudo()
        self.sudo().search([("channel_id", "=", channel.id), ("source_type", "in", ("slide", "course"))]).unlink()
        base_url = channel.get_base_url()
        vals_list = [{
            "name": channel.name,
            "content": html2plaintext(channel.description or "") or channel.name,
            "source_type": "course",
            "channel_id": channel.id,
            "url": base_url + (channel.website_url or ""),
        }]
        for slide in channel.slide_ids.filtered(lambda s: not s.is_category):
            parts = [html2plaintext(slide.description or "")]
            if "html_content" in slide._fields and slide.html_content:
                parts.append(html2plaintext(slide.html_content))
            for q in slide.question_ids:
                correct = ", ".join(q.answer_ids.filtered("is_correct").mapped("text_value"))
                parts.append(_("Quiz: %(q)s Answer: %(a)s", q=q.question, a=correct))
            text = "\n\n".join(p for p in parts if p and p.strip())
            for i, chunk in enumerate(split_chunks(text)):
                vals_list.append({
                    "name": slide.name, "sequence": i, "content": chunk, "source_type": "slide",
                    "channel_id": channel.id, "slide_id": slide.id,
                    "url": base_url + (slide.website_url or ""),
                })
        chunks = self._index_records(vals_list)
        channel.eduagent_last_indexed = fields.Datetime.now()
        return chunks

    @api.model
    def index_faqs(self):
        self.sudo().search([("source_type", "=", "faq")]).unlink()
        faqs = self.env["eduagent.faq"].sudo().search([])
        return self._index_records([{
            "name": f.name, "content": html2plaintext(f.answer or ""), "source_type": "faq",
            "faq_id": f.id, "channel_id": f.channel_id.id or False} for f in faqs])

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------
    @api.model
    def retrieve(self, query, channel_ids=None, source_types=None, limit=4, min_ratio=0.0):
        """Return the best chunks; ``min_ratio`` drops chunks scoring below ratio * best score."""
        domain = [("company_id", "in", [False, self.env.company.id])]
        if channel_ids:
            domain += ["|", ("channel_id", "in", list(channel_ids)), ("channel_id", "=", False)]
        if source_types:
            domain += [("source_type", "in", list(source_types))]
        chunks = self.sudo().search(domain, limit=2000)
        if not chunks or not (query or "").strip():
            return chunks[:limit]
        qvec = None
        if any(chunks.mapped("embedding")):
            vecs = self.env["eduagent.llm"].embed([query])
            qvec = vecs[0] if vecs else None
        scored = []
        if qvec:
            for c in chunks:
                if c.embedding:
                    scored.append((cosine(qvec, json.loads(c.embedding)), c.id))
        else:
            qtok = tokenize(query)
            if not qtok:
                return chunks[:limit]
            n = len(chunks)
            docs = {c.id: Counter((c.keywords or "").split()) for c in chunks}
            df = Counter()
            for counts in docs.values():
                for t in set(qtok):
                    if t in counts:
                        df[t] += 1
            for cid, counts in docs.items():
                length = sum(counts.values()) or 1
                score = 0.0
                for t in qtok:
                    tf = counts.get(t, 0)
                    if tf:
                        idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                        score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * length / 120.0))
                if score > 0:
                    scored.append((score, cid))
        scored.sort(reverse=True)
        if scored and min_ratio:
            best = scored[0][0]
            scored = [(sc, cid) for sc, cid in scored if sc >= best * min_ratio]
        return self.browse([cid for _s, cid in scored[:limit]])

    @api.model
    def format_context(self, chunks):
        lines = []
        for i, c in enumerate(chunks, 1):
            lines.append("[S%d] %s%s\n%s" % (i, c.name, (" (%s)" % c.url) if c.url else "", c.content[:1500]))
        return "\n\n".join(lines)
