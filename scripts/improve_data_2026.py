"""Acquire curated autumn sources and remove unsupported candidate deadlines."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from schemas import Competition
from trust import _evidence_is_complete, assess_source_readiness, assess_recommendation_readiness

SAMPLES = ROOT / "data/ground_truth/samples"
SOURCES = ROOT / "data/official_sources/autumn_2026"
TODAY = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
APM_IMAGE = "https://files.apmcm.org/uploads/images/20260915/1789456047535603.png"
CMIT = "https://www.cmit.cn/wap/tongzhigonggao/show-144-45826-1.html"
CMIT_GUIDE = "https://static.cmit.cn/uploadfile/2026/0924/附件1 2026年第十四届全国大学生数字媒体科技作品及创意竞赛参赛指南.pdf"


def normalize(text):
    return "".join(text.split())


def acquire(name, url):
    response = requests.get(url, timeout=45)
    response.raise_for_status()
    payload = response.content
    if name.endswith(".pdf") and not payload.startswith(b"%PDF"):
        raise ValueError("Expected official PDF")
    (SOURCES / name).write_bytes(payload)
    return payload


def evidence(field, quote, name, url, payload):
    if name.endswith(".html"):
        document = html.fromstring(payload.decode("utf-8"))
        for element in document.xpath("//script|//style|//noscript"):
            element.drop_tree()
        if normalize(quote) not in normalize(document.text_content()):
            raise ValueError(f"Official quote absent: {name}/{field}")
    return {"field": field, "source_text": quote, "page": None,
            "document_name": name, "source_url": url, "trusted_level": "A",
            "document_sha256": hashlib.sha256(payload).hexdigest(),
            "acquired_date": TODAY, "last_verified_at": TODAY}


def record(cid, name, organizer, category, url, deadline, students, low, high, materials, skills, quotes, source_name, payload, **extra):
    raw = {"competition_id": cid, "competition_name": name, "document_year": 2026,
           "organizer": organizer, "category": category, "official_source_url": url,
           "official_source_status": "found", "source_acquired_date": TODAY,
           "last_verified_at": TODAY, "doc_version": "2026_autumn_20261003",
           "eligible_students": students, "allowed_grades": None, "allowed_majors": None,
           "team_required": high != 1, "team_min": low, "team_max": high,
           "registration_deadline": deadline, "submission_deadline": None,
           "required_materials": materials, "required_skills": skills,
           "evaluation_dimensions": [], "data_status": "verified", "trusted_level": "A",
           "evidence": [evidence(f, q, source_name, url, payload) for f, q in quotes.items()], **extra}
    assessment = assess_source_readiness(Competition.model_validate(raw))
    if not assessment.ready:
        raw.update(data_status="unverified", trusted_level="B")
    return raw


def curate():
    SOURCES.mkdir(parents=True, exist_ok=True)
    records = []
    url = "https://www.nmmcm.org.cn/Competition/"
    name = "swb_autumn_2026_official.html"
    payload = acquire(name, url)
    team = "全国普通高校全日制在校生（分专科组、本科生组、研究生组）。以队为单位报名，每队1–3名学生"
    records.append(record("swb_autumn_2026", "2026年第十二届数维杯全国大学生数学建模挑战赛（秋季赛）",
        "中国仿真学会、辽宁科技大学", "modeling", url, "2026-11-20", ["本科生", "研究生", "专科生"], 1, 3,
        ["电子版PDF格式英文论文（按题号与队伍编号命名）"], ["数学建模", "Python", "论文写作", "英语"],
        {"registration_deadline": "报名截止：即日起至2026年11月20日06:00",
         "registration_deadline_at": "报名截止：即日起至2026年11月20日06:00",
         "eligible_students": team, "team_min": team, "team_max": team,
         "required_materials": "参赛队伍仅须提交电子版PDF格式论文，无需另附纸质文件。",
         "submission_deadline": "论文提交截止：2026年11月24日10:00（逾期提交无效）",
         "submission_deadline_at": "论文提交截止：2026年11月24日10:00（逾期提交无效）",
         "competition_end_date": "竞赛时间：2026年11月20日09:00—11月24日09:00",
         "fee": "每队缴纳报名费200元"}, name, payload,
        registration_deadline_at="2026-11-20T06:00:00+08:00", submission_deadline="2026-11-24",
        submission_deadline_at="2026-11-24T10:00:00+08:00", deadline_timezone_basis="campus_default",
        competition_start_date="2026-11-20", competition_end_date="2026-11-24",
        brief_description="秋季英文论文赛，允许跨校组队，报名截止与竞赛结束、论文提交为三个不同时间。",
        notes="独立于春季赛；原文未说明时区，精确时刻采用校园UTC+08:00假设。论文须符合官网规范及原创性要求；最终以官网最新通知为准。"))

    url = "https://www.apmcm.org/detail/2512"
    acquire("apmcm_autumn_2026_notice.html", url)
    name = "apmcm_autumn_2026_official.png"
    payload = acquire(name, APM_IMAGE)
    if hashlib.sha256(payload).hexdigest() != "7772500856495bc467e1c423015f8b282188f38bcd1700a31a7f0a03aebc807a":
        raise ValueError("Official image changed; visually recheck before reusing curated quotations")
    team = "参赛对象为普通高校全日制在校大学生，参赛队由1–3名大学生组成。"
    raw = record("apmcm_autumn_2026", "2026年第十六届APMCM亚太地区大学生数学建模竞赛（秋季英文赛项）",
        "中国国际科技促进会物联网工作委员会、北京图象图形学学会", "modeling", APM_IMAGE,
        "2026-11-25", ["本科生", "研究生", "专科生"], 1, 3, ["电子版英文论文"],
        ["数学建模", "Python", "英语", "论文写作"],
        {"registration_deadline": "报名截止日期为11月25日，报名截止后不能再更改报名信息。",
         "eligible_students": "竞赛分为研究生组、本科组、专科组，报名时请根据参赛队员中最高在读学历选择组别。",
         "team_min": team, "team_max": team,
         "required_materials": "竞赛只需要提交电子版论文，不需要邮寄纸质版论文；所有参赛队必须提交英文版论文。",
         "competition_end_date": "竞赛的时间确定为2026年11月26日06:00至2026年11月30日09:00。"},
        name, payload, competition_start_date="2026-11-26", competition_end_date="2026-11-30",
        brief_description="秋季英文赛项，与现有六月中文赛项独立；允许跨校组队，每队最多一名指导教师。",
        notes="原文来自官网通知所链接的盖章图片，经逐项视觉读取；不是OCR自动核验。报名仅给日期，未伪造截止钟点。提交截止细则尚待补充，不将竞赛结束自动当作论文提交截止。最终以官网最新通知为准。")
    raw["official_source_url"] = url
    records.append(raw)

    name = "dmt_2026_official.html"
    payload = acquire(name, CMIT)
    acquire("dmt_2026_guide.pdf", CMIT_GUIDE)
    raw = record("dmt_2026", "2026年第十四届全国大学生数字媒体科技作品及创意竞赛（在校学生自主选题）",
        "全国大学生数字媒体科技作品及创意竞赛组委会", "design", CMIT, "2026-10-23",
        ["本科生", "研究生", "专科生"], None, 5,
        ["作品本体或可运行文件", "作品说明文档", "作品演示视频", "图像佐证材料", "原创性声明", "知识产权说明", "人工智能工具使用说明（使用时）"],
        ["数字媒体", "视频制作", "设计", "软件开发"],
        {"registration_deadline": "赛程时间：截至2026年10月23日23:59:59",
         "registration_deadline_at": "赛程时间：截至2026年10月23日23:59:59",
         "submission_deadline": "赛程时间：截至2026年10月23日23:59:59",
         "submission_deadline_at": "赛程时间：截至2026年10月23日23:59:59",
         "eligible_students": "本次竞赛报名面向普通高等学校全日制在校学生开放，参赛学生范围包括研究生、本科生及高职高专学生，参赛专业不受限制。",
         "team_max": "每个参赛团队的学生人数不超过五人",
         "required_materials": "参赛作品材料通常包含作品本体或可运行文件、作品说明文档、作品演示视频、图像佐证材料、原创性声明、知识产权说明以及人工智能工具使用说明等类别。"},
        name, payload, registration_deadline_at="2026-10-23T23:59:59+08:00", submission_deadline="2026-10-23",
        submission_deadline_at="2026-10-23T23:59:59+08:00", deadline_timezone_basis="campus_default",
        brief_description="面向全日制在校学生的数字媒体自主选题赛事，具体材料按所选赛道核对附件。",
        notes="删除旧的无证据主办方、奖项比例及仅限大一至大四条件。毕业五年内团队仅可报指定开放命题，不纳入本记录通用资格。指南PDF为扫描件，保存原件供用户查看；人数下限未明确，保持未知并维持候选。时区为校园假设，最终以官网最新通知为准。")
    records.append(raw)
    for raw in records:
        Competition.model_validate(raw)
        (SAMPLES / (raw["competition_id"] + ".json")).write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    urls = {"swb_autumn_2026_official.html": "https://www.nmmcm.org.cn/Competition/",
            "apmcm_autumn_2026_notice.html": "https://www.apmcm.org/detail/2512",
            "apmcm_autumn_2026_official.png": APM_IMAGE, "dmt_2026_official.html": CMIT,
            "dmt_2026_guide.pdf": CMIT_GUIDE}
    manifest = [{"document_path": p.relative_to(ROOT).as_posix(), "source_url": urls[p.name], "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                 "acquired_date": TODAY, "source_checked_date": TODAY} for p in sorted(SOURCES.iterdir()) if p.is_file() and p.name != "manifest.json"]
    (SOURCES / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [r["competition_id"] for r in records]


def clean_candidate_dates(raw):
    if raw.get("data_status") == "verified":
        return {}
    comp = validated(raw)
    supported = {e.field for e in comp.evidence if _evidence_is_complete(e)}
    removed = {}
    for field in ("registration_deadline", "registration_deadline_at", "submission_deadline", "submission_deadline_at"):
        if raw.get(field) is not None and field not in supported:
            removed[field] = raw[field]
            raw[field] = None
    if removed:
        raw["notes"] = (raw.get("notes") or "") + " 无对应完整官方原文的报名/提交日期已清空为未知；不根据往年规律或候选占位日期推断。"
    return removed


def validated(raw):
    normalized = dict(raw)
    for field in ("evaluation_dimensions", "required_skills", "required_materials", "evidence", "eligible_students", "award_distribution"):
        if normalized.get(field) is None:
            normalized[field] = []
    return Competition.model_validate(normalized)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    curated = curate() if args.write else []
    audit = []
    counts = {"total": 0, "source_complete": 0, "registerable": 0, "official_found": 0}
    for path in sorted(SAMPLES.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("competition_id", path.stem)
        removed = clean_candidate_dates(raw)
        if removed:
            audit.append({"competition_id": path.stem, "removed_unsupported_dates": removed})
            if args.write:
                path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        comp = validated(raw)
        counts["total"] += 1
        counts["source_complete"] += assess_source_readiness(comp).ready
        counts["registerable"] += assess_recommendation_readiness(comp, datetime.fromisoformat("2026-10-03T17:00:00+08:00")).ready
        counts["official_found"] += comp.official_source_status == "found"
    report = {"date": TODAY, "mode": "written" if args.write else "dry-run", "curated": curated,
              "counts": counts, "cleaned_records": len(audit), "removed_date_fields": sum(len(r["removed_unsupported_dates"]) for r in audit),
              "audit": audit}
    if args.write:
        destination = ROOT / "evals/data_quality_results.json"
        previous = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {}
        merged = {item["competition_id"]: item for item in previous.get("audit", [])}
        for item in audit:
            existing = merged.setdefault(item["competition_id"], {"competition_id": item["competition_id"], "removed_unsupported_dates": {}})
            existing["removed_unsupported_dates"].update(item["removed_unsupported_dates"])
        report["audit"] = sorted(merged.values(), key=lambda item: item["competition_id"])
        report["cleaned_records"] = len(merged)
        report["removed_date_fields"] = sum(len(item["removed_unsupported_dates"]) for item in merged.values())
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "audit"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
