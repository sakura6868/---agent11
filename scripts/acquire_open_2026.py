"""Acquire reviewed, still-open tracks; every quote must occur in its source snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import pdfplumber
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from schemas import Competition
from trust import assess_source_readiness

STUDENTS = ["本科生", "研究生", "专科生"]
TRACKS = [
    dict(id="aic_energy_2026", name="第八届AIC算法大赛—AI+能源主题赛（科技创新组）", category="robotics_ai",
         url="https://www.aicomp.cn/tracks/4726.html", cache="aic_energy", deadline="2026-10-15", team=True, maximum=3,
         students="参赛成员须为国内外高校（含研究所）在读学生，包括研究生、本科生、专科生（高职/高专），不包含任何企业人员或非在校学生。",
         team_quote="参赛选手可单人创建队伍参赛，也可与其他选手组队参赛，不得跨学校组队（含分校），每支团队成员上限3名。",
         deadline_quote="报名截止时间为10月15日20:00。", submission_quote="初赛作品提交截止时间为10月15日20:00。",
         material_quotes=["可运行的代码、模型或系统原型（提供数据，并附详细部署说明）", "技术报告（PDF格式）、汇报PPT、演示视频（5分钟以内，MP4格式）"],
         materials=["可运行代码、模型或系统原型及数据、部署说明", "技术报告（PDF）", "汇报PPT", "演示视频（MP4，5分钟以内）"],
         skills=["Python", "机器学习", "能源系统"], notes="仅科技创新组；同校组队，每队500元。硬件照片/录像为涉及硬件时的可选附件。"),
    dict(id="aic_interconnect_2026", name="第八届AIC算法大赛—智能体互联主题赛", category="software",
         url="https://www.aicomp.cn/tracks/tracks-5/5169.html", cache="aic_interconnect", deadline="2026-10-15", team=True, maximum=3,
         students="全球高校和科研院所拥有正式学籍的在读学生（含研究生、本科生、专科生）均可报名参赛。",
         team_quote="参赛选手可单人创建队伍参赛，也可与其他选手组队参赛，每支团队成员上限3名。",
         deadline_quote="报名截止时间为10月15日20:00。", submission_quote="初赛作品提交截止时间为10月15日20:00。",
         material_quotes=["参赛成果需包含可运行工程代码与完整技术报告", "并在技术报告与演示视频中提交平台后台访问数据、运行截图及互联交互记录等佐证材料"],
         materials=["可运行工程代码", "完整技术报告", "演示视频", "梧桐平台后台访问数据、运行截图和互联交互记录"],
         skills=["Python", "智能体开发", "接口开发"], notes="须接入梧桐智能体互联平台，完成注册、自动发现、跨端访问验证；每队500元。"),
    dict(id="aic_6g_2026", name="第八届AIC算法大赛—人工智能+6G青年创新创业专项赛（学生组）", category="innovation",
         url="https://www.aicomp.cn/tracks/tracks-4/5322.html", cache="aic_6g", deadline="2026-11-13", team=True, maximum=3,
         students="全球各高等院校、科研单位、企事业单位、初创团队、个人等均可免费报名参赛。",
         team_quote="学生组：参赛选手可单人创建队伍参赛，也可与本校（不含分校）其他选手组队参赛，每支团队成员上限3名（跨校组队无效）。",
         deadline_quote="报名及作品提交截止日期为11月13日20:00。", submission_quote="报名及作品提交截止日期为11月13日20:00。",
         material_quotes=[], materials=["作品名称与简介", "作品方案（PDF）", "演示视频（MP4，3—5分钟）", "答辩PPT（PDF）", "佐证材料（PDF）", "其他材料分享链接"],
         skills=["人工智能", "通信技术", "技术报告"], notes="仅学生组，同校组队，免费报名。作品材料不得出现单位名称、LOGO和指导教师信息；须披露生成式AI使用。企业组规则不混入学生组。"),
    dict(id="ncccu_office_2026", name="2026年第八届全国高校计算机能力挑战赛—Office高级应用赛（区域赛/省赛）", category="design",
         url="https://www.ncccu.org.cn/index/Paper/case3.html", cache="ncccu_office", deadline="2026-11-25", team=False, maximum=1,
         students="大赛的参赛对象是高校所有专业在校生，本研组（本科、研究生）和高职组（高职、高专）分别评奖。",
         team_quote="区域赛赛段个人赛各科目收取报名、考试及评审费人民币80元/科。",
         deadline_quote="区域赛/省赛报名：2026年7月9日—11月25日", submission_quote=None,
         material_quotes=["如发现提交他人的主观题答卷，一律按0分处理。"],
         materials=["在线考试答卷（按所选科目完成，非提前作品上传制）"],
         skills=["Word", "Excel", "PowerPoint"], notes="个人赛，人数1由官方个人赛形式确定，不是未知人数默认值。Word/Excel/PowerPoint/WPS智能应用分别报名；80元/科，区域赛11月28日。"),
    dict(id="ncccu_program_2026", name="2026年第八届全国高校计算机能力挑战赛—程序设计挑战赛（区域赛/省赛）", category="programming",
         url="https://www.ncccu.org.cn/index/Paper/case4.html", cache="ncccu_program", deadline="2026-11-25", team=False, maximum=1,
         students="大赛的参赛对象是高校所有专业在校生，本研组（本科、研究生）和高职组（高职、高专）分别评奖。",
         team_quote="区域赛赛段个人赛各科目收取报名、考试及评审费人民币80元/科。",
         deadline_quote="区域赛/省赛报名：2026年7月9日—11月25日", submission_quote=None,
         material_quotes=["编程题不设提交次数限制。"], materials=["在线选择题答案与编程题代码（按报名语言科目）"],
         skills=["Python", "数据结构", "算法"], notes="个人赛，人数1由官方个人赛形式确定。C/C++/Java/Python分科报名；80元/科，区域赛11月29日。"),
]


def visible_text(content: bytes) -> str:
    root = html.fromstring(content.decode("utf-8"))
    for element in root.xpath("//script|//style|//noscript|//svg"):
        element.drop_tree()
    return " ".join(root.text_content().split())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewed-cache", type=Path, help="Reuse previously downloaded official snapshots")
    parser.add_argument("--acquired-date", default=datetime.now(timezone(timedelta(hours=8))).date().isoformat())
    args = parser.parse_args()
    destination = ROOT / "data/official_sources/open_2026"
    destination.mkdir(parents=True, exist_ok=True)
    manifest = []
    # Validate all records before writing any Ground Truth.
    records = []
    for track in TRACKS:
        if args.reviewed_cache:
            content = (args.reviewed_cache / (track["cache"] + ".html")).read_bytes()
        else:
            response = requests.get(track["url"], timeout=30)
            response.raise_for_status()
            content = response.content
        document = track["id"] + "_official.html"
        digest = hashlib.sha256(content).hexdigest()
        text = visible_text(content)
        evidence = []

        def add(field, quote, source=track["url"], name=document, fingerprint=digest, page=None, haystack=text):
            if " ".join(quote.split()) not in " ".join(haystack.split()):
                raise ValueError(f"Official quote missing: {track['id']}/{field}: {quote}")
            evidence.append(dict(field=field, source_text=quote, source_url=source, document_name=name,
                                 document_sha256=fingerprint, page=page, trusted_level="A",
                                 acquired_date=args.acquired_date, last_verified_at=args.acquired_date))

        add("eligible_students", track["students"])
        for field in ("team_min", "team_max"):
            add(field, track["team_quote"])
        add("registration_deadline", track["deadline_quote"])
        if track["team"]:
            add("registration_deadline_at", track["deadline_quote"])
            add("submission_deadline", track["submission_quote"])
            add("submission_deadline_at", track["submission_quote"])
        for quote in track["material_quotes"]:
            add("required_materials", quote)
        if track["id"] == "aic_6g_2026":
            link_root = html.fromstring(content.decode("utf-8"))
            pdf_url = next(a.get("href") for a in link_root.xpath("//a[@href]")
                           if a.text_content().strip() == "人工智能+6G青年创新创业专项赛作品提交要求")
            if args.reviewed_cache:
                pdf_content = (args.reviewed_cache / "aic_6g_requirements.pdf").read_bytes()
            else:
                response = requests.get(pdf_url, timeout=30)
                response.raise_for_status()
                pdf_content = response.content
            import io
            with pdfplumber.open(io.BytesIO(pdf_content)) as pdf:
                quote = "作品材料包括：作品名称、作品简介、作品方案、演示视频、答辩PPT、佐证材料、作品其他材料链接等，提交规范如下："
                page_text = pdf.pages[0].extract_text() or ""
                # PDF line wrapping may split a Chinese word; preserve the actual extracted quotation.
                begin = page_text.index("作品材料包括：")
                end = page_text.index("提交规范如下：", begin) + len("提交规范如下：")
                add("required_materials", page_text[begin:end], source=pdf_url, name="aic_6g_requirements.pdf",
                    fingerprint=hashlib.sha256(pdf_content).hexdigest(), page=1, haystack=page_text)
            (destination / "aic_6g_requirements.pdf").write_bytes(pdf_content)
            manifest.append(dict(source_url=pdf_url, document_path="data/official_sources/open_2026/aic_6g_requirements.pdf",
                                 sha256=hashlib.sha256(pdf_content).hexdigest(), acquired_date=args.acquired_date))
        raw = dict(competition_id=track["id"], competition_name=track["name"], document_year=2026,
                   organizer="全球校园人工智能算法精英大赛全国组委会" if track["id"].startswith("aic_") else "全国高等学校计算机教育研究会",
                   category=track["category"], eligible_students=STUDENTS, team_required=track["team"], team_min=1, team_max=track["maximum"],
                   registration_deadline=track["deadline"], submission_deadline=track["deadline"] if track["team"] else None,
                   registration_deadline_at=track["deadline"] + "T20:00:00+08:00" if track["team"] else None,
                   submission_deadline_at=track["deadline"] + "T20:00:00+08:00" if track["team"] else None,
                   deadline_timezone_basis="campus_default", required_materials=track["materials"], required_skills=track["skills"],
                   official_source_url=track["url"], official_source_status="found", source_acquired_date=args.acquired_date,
                   last_verified_at=args.acquired_date, doc_version="2026_official_" + args.acquired_date,
                   data_status="verified", trusted_level="A", evidence=evidence,
                   brief_description=track["name"], notes=track["notes"] + " 时刻未明示时区时使用校园UTC+08:00假设；最终以官网及学校审核为准。")
        assessment = assess_source_readiness(Competition.model_validate(raw))
        if not assessment.ready:
            raise ValueError(assessment.reasons)
        records.append((raw, document, content))
        manifest.append(dict(competition_id=track["id"], source_url=track["url"],
                             document_path="data/official_sources/open_2026/" + document, sha256=digest,
                             acquired_date=args.acquired_date, source_checked_date=args.acquired_date))
    for raw, document, content in records:
        (destination / document).write_bytes(content)
        (ROOT / "data/ground_truth/samples" / (raw["competition_id"] + ".json")).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(raw["competition_id"], "official quotes checked; strict readiness passed")
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
