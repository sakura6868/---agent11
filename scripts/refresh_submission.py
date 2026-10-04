"""Generate the review snapshot from checked-in data and actual evaluation reports."""
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT))
from schemas import Competition
from evals.run_eval import load_competitions
from trust import assess_source_readiness, is_registerable_now

def main():
    records=load_competitions(ROOT)
    current=datetime.fromisoformat('2026-10-04T01:00:00+08:00')
    ready=[c for c in records if assess_source_readiness(c).ready]
    opened=[c for c in ready if is_registerable_now(c,current)]
    formal=json.loads((ROOT/'evals/formal_results.json').read_text(encoding='utf-8-sig'))
    templates=json.loads((ROOT/'evals/metrics.json').read_text(encoding='utf-8-sig'))
    browser=json.loads((ROOT/'evals/planning_browser_results.json').read_text(encoding='utf-8-sig'))
    video=ROOT/'demo/演示视频.mp4'
    report=dict(version=(ROOT/'VERSION').read_text().strip(),snapshot_at=current.isoformat(),records=len(records),
      official_source_found=sum(c.official_source_status=='found' for c in records),source_ready=len(ready),
      open_source_ready=len(opened),open_ids=[c.competition_id for c in opened],formal=formal['formal_summary'],
      regression=formal['regression']['tests_run'],subtests=formal['regression']['subtests_passed'],
      templates=dict(passed=templates['passed'],total=templates['total_cases']),browser=browser['summary'],
      evaluation_fixture_date=formal['fixture_date'],dataset_sha256=formal['dataset_sha256'],
      video_sha256=hashlib.sha256(video.read_bytes()).hexdigest() if video.exists() else None,
      real_user_validation=False,live_model_planning_validated=False,cloud_rotation_verified=False)
    (ROOT/'evals/submission_snapshot.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    text=f'''# 当前提交快照

版本：{report['version']}。目录统计时刻：{current.isoformat()}。唯一机器可读口径为 [submission_snapshot.json](../evals/submission_snapshot.json)。本版仅附当前验收报告。

| 项目 | 本版实测 |
|---|---|
| 赛事目录 | {len(records)} 条，不能全部正式推荐 |
| 找到官方来源 | {report['official_source_found']} 条；有来源不等于证据齐全 |
| 通过严格关键证据门控 | {len(ready)} 条，含历史赛事 |
| 上述统计时刻仍可报名 | {len(opened)} 条；实际名单随时间变化 |
| 正式独立标注用例 | {report['formal']['passed']}/{report['formal']['total']} |
| 回归测试 | {report['regression']} 通过，另 {report['subtests']} 个子测试 |
| 自动生成模板回归 | {report['templates']['passed']}/{report['templates']['total']}，不作为独立标注准确率 |
| 实际浏览器规划检查 | {report['browser']['passed']}/{report['browser']['total']}，桌面及 390px 手机 |

正式与模板评测使用固定日期 {report['evaluation_fixture_date']}；目录开放统计使用上面的提交时刻。两者用途不同。详见 [定量评测](QUANTITATIVE_EVALUATION.md)、[模板报告](../evals/report.md) 和 [浏览器报告](../evals/planning_browser_results.json)。

新增五个赛道均保存官方 HTML/PDF、字段原文定位、获取日期与 SHA256；见 [新增来源清单](../data/official_sources/open_2026/manifest.json)。同一大赛的不同赛道分别统计，不能说成五个独立大赛。DMT 队伍下限未知，继续作为候选，未为了扩大推荐覆盖补造规则。

参赛规划通过六个有状态工具完成目标检索、证据检查、资格检查、机会比较、组合求解和执行清单。可用工具由前一步观察决定；模型最多参与两次工具选择，非法选择回退到离线策略。当前浏览器验收使用离线策略，在线规划模型效果尚未验证。组合工作量和收益是启发式估计。规划本身不写画像、不报名、不自动新增项目；用户确认画像并点击采用后才写个人项目。

当前演示视频：[演示视频.mp4](../demo/演示视频.mp4)，采用用户指定原片，约3分28秒（208.05秒），未剪辑、转码或重新配音。视频交付说明见 [视频说明](../submission/04_演示视频交付说明.md)；录制脚本及旁白稿仅为复录参考。

生产启动检查拒绝短密钥、占位密钥、用户与管理员共用密钥、调试管理员登录及通配 CORS；生产默认禁用演示账号。默认不配置云端数据库或模型密钥，源码与归档有凭据模式扫描。云端凭据管理与生产验收不包含在离线验证范围内，`cloud_rotation_verified=false` 不代表平台状态已被验证。

本次不包含真实学生试用研究；不宣称提升获奖率、节省多少时间或线上新版本已经部署。自动来源扫描需要平台配置，人工审核之前不改写正式赛事事实。
'''
    (ROOT/'docs/SUBMISSION_STATUS.md').write_text(text,encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('records','official_source_found','source_ready','open_source_ready','templates','browser')},ensure_ascii=False))

if __name__=='__main__': main()
