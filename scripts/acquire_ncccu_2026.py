"""Acquire three independent NCCCU tracks; refuse quotes absent from official HTML."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import requests
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from schemas import Competition
from trust import assess_source_readiness

STUDENT_QUOTE = "大赛的参赛对象是高校所有专业在校生，本研组（本科、研究生）和高职组（高职、高专）分别评奖。"
TEAM_QUOTE = "每支参赛队伍限1—3名队员"
PREDICTION_QUOTE = "初赛仅提交预测结果csv文件，复赛需要同时提交预测结果csv文件及模型以供验证"
TRACKS = [
    {
        "id": "ncccu_data_2026", "case": 2, "name": "大数据挑战赛（区域赛/省赛）",
        "category": "data", "deadline": "2026-11-06", "start": "2026-10-13",
        "registration_quote": "报名时间：即日起—2026年11月6日",
        "submission_quote": "区域赛/省赛作品截止提交时间：11月6日23:59",
        "materials": ["预测结果 submission.csv（UTF-8，初赛）"],
        "materials_quote": PREDICTION_QUOTE,
        "skills": ["Python", "机器学习", "数据分析"],
        "description": "基于眼底图像的分类竞赛；初赛仅提交预测结果，复赛另需模型。",
        "notes": "每日最多2次提交；不得使用外部标注数据；模型总大小不超过500MB，单张图像端到端延迟不超过100ms。医学主题仅为竞赛任务，不提供医疗建议。",
    },
    {
        "id": "ncccu_ai_2026", "case": 1, "name": "人工智能挑战赛（区域赛/省赛）",
        "category": "robotics_ai", "deadline": "2026-11-13", "start": "2026-10-15",
        "registration_quote": "报名时间：即日起—2026年11月13日",
        "submission_quote": "区域赛/省赛作品截止提交时间：11月13日23:59",
        "materials": ["预测结果 submission.csv（UTF-8，初赛）"],
        "materials_quote": PREDICTION_QUOTE,
        "skills": ["Python", "深度学习", "数据分析"],
        "description": "基于胸部X光片的多标签分类竞赛；初赛仅提交预测结果，复赛另需模型。",
        "notes": "每日最多2次提交；不得使用外部标注数据；模型总大小不超过500MB，单张图像端到端延迟不超过100ms。医学主题仅为竞赛任务，不提供医疗建议。",
    },
    {
        "id": "ncccu_digital_2026", "case": 7, "name": "数字媒体创新设计赛（区域赛/省赛）",
        "category": "design", "deadline": "2026-11-19", "start": "2026-08-06",
        "registration_quote": "报名时间：2026年7月9日—11月19日",
        "submission_quote": "区域赛设计与提交时间：2026年8月6日—11月19日",
        "materials": ["设计说明文档（500字以内）", "作品文件或链接（按所选科目）", "海报源文件（仅海报设计科目）"],
        "materials_quote": "要求参赛队伍根据大赛主题进行作品创作并在截止时间前将设计说明、作品或链接上传至大赛官网参赛页面。",
        "skills": ["视觉设计", "视频制作", "设计说明"],
        "description": "海报、动画、视频、AIGC创意设计四科目分别报名评比；制作阶段已开始但报名仍开放。",
        "notes": "报名时按科目核对文件格式与大小。海报设计须上传源文件；海报、视频、动画科目禁止AIGC工具，使用AI须选AIGC科目并披露工具用途、输入素材和参数。作品不得出现学校、团队、成员姓名；须有赛事Logo水印及合法素材版权。国赛另有PPT答辩，不混入初赛材料。",
    },
]


def visible_text(content: bytes) -> str:
    root = html.fromstring(content.decode("utf-8"))
    for element in root.xpath("//script|//style|//noscript|//svg|//nav|//footer|//header"):
        element.drop_tree()
    return " ".join(root.text_content().split())


def build_record(track: dict, content: bytes, acquired: str) -> dict:
    url = f"https://www.ncccu.org.cn/index/Paper/case{track['case']}.html"
    document = f"{track['id']}_official.html"
    digest = hashlib.sha256(content).hexdigest()
    text = visible_text(content)
    quotes = {
        "registration_deadline": track["registration_quote"],
        "eligible_students": STUDENT_QUOTE,
        "team_min": TEAM_QUOTE,
        "team_max": TEAM_QUOTE,
        "required_materials": track["materials_quote"],
        "submission_deadline": track["submission_quote"],
    }
    if track["case"] in {1, 2}:
        quotes["submission_deadline_at"] = track["submission_quote"]
    if track["case"] == 7:
        quotes["required_materials_source"] = "需上传源文件，大小不超过500M。并附上500字以内的设计说明。"
    evidence = []
    for field, quote in quotes.items():
        if " ".join(quote.split()) not in text:
            raise ValueError(f"Official quote missing: {track['id']}/{field}")
        evidence.append({
            "field": field, "page": None, "source_text": quote, "source_url": url,
            "document_name": document, "document_sha256": digest,
            "acquired_date": acquired, "last_verified_at": acquired, "trusted_level": "A",
        })
    raw = {
        "competition_id": track["id"],
        "competition_name": "2026年第八届全国高校计算机能力挑战赛—" + track["name"],
        "document_year": 2026, "organizer": "全国高等学校计算机教育研究会",
        "category": track["category"], "eligible_students": ["本科生", "研究生", "专科生"],
        "allowed_grades": None, "allowed_majors": None,
        "team_required": True, "team_min": 1, "team_max": 3,
        "registration_deadline": track["deadline"], "registration_deadline_at": None,
        "submission_deadline": track["deadline"],
        "submission_deadline_at": track["deadline"] + "T23:59:00+08:00" if track["case"] in {1, 2} else None,
        "deadline_timezone_basis": "campus_default",
        "competition_start_date": track["start"], "competition_end_date": track["deadline"],
        "required_materials": track["materials"], "required_skills": track["skills"],
        "brief_description": track["description"], "official_source_url": url,
        "official_source_status": "found", "source_acquired_date": acquired,
        "last_verified_at": acquired, "data_status": "verified", "trusted_level": "A",
        "doc_version": f"2026_official_html_{acquired}", "evidence": evidence,
        "notes": track["notes"] + " 每队200元；各院校不超过100队，需向学校确认名额，允许跨校。记录只覆盖区域赛/省赛，不替代官网报名审核。报名原文未给具体时刻，不推定为23:59；提交时刻未给时区时使用校园UTC+08:00假设，最终以官网最新通知为准。",
    }
    model = Competition.model_validate(raw)
    readiness = assess_source_readiness(model)
    if not readiness.ready:
        raise ValueError(readiness.reasons)
    return raw


def main() -> None:
    acquired = date.today().isoformat()
    source_dir = ROOT / "data/official_sources/ncccu_2026"
    source_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for track in TRACKS:
        url = f"https://www.ncccu.org.cn/index/Paper/case{track['case']}.html"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        record = build_record(track, response.content, acquired)
        document = source_dir / f"{track['id']}_official.html"
        document.write_bytes(response.content)
        destination = ROOT / f"data/ground_truth/samples/{track['id']}.json"
        destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest.append({"competition_id": track["id"], "source_url": url,
                         "snapshot": document.relative_to(ROOT).as_posix(),
                         "sha256": hashlib.sha256(response.content).hexdigest(),
                         "acquired_date": acquired, "source_checked_date": acquired})
        print(track["id"], "official quotes present; source readiness passed")
    (source_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
