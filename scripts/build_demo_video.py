"""Narrate and assemble a real browser recording using Windows local speech."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import shutil
import wave
from pathlib import Path
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = [
 ("首页与画像", "校园科创导航，把选择比赛转为可执行行动。这次演示使用隔离数据库和虚构学生画像。当前功能与测试结果均以十月四日提交快照为准。"),
 ("保存容量约束", "先确认学生画像。这里设置每周四十小时、三人队伍。规划会同时计算已有项目的工作量。新增时间约束只有经过确认，才会写入个人画像。"),
 ("官方证据扩充", "新增五个来源完整的开放赛道，覆盖人工智能、能源、六Ｇ、办公技能与程序设计。候选资料仍然不能进入正式推荐，规则缺失不会补默认值。"),
 ("目标驱动规划", "选择参赛规划，输入最多参加两场比赛。智能体依次检索、核验证据、检查资格、比较机会、优化组合和生成清单。当前演示使用离线策略。"),
 ("工具轨迹与执行清单", "结果同时展示工具观察、赛事证据和任务清单。启用模型时，模型可以在允许的工具中选择下一步，最多两次调用。无效选择会回退，规划不会自动报名。"),
 ("零容量的停止条件", "把每周时间改为零，智能体停止给出可采用方案。它不会虚构时间或放宽资格。临时条件也不会改写已经保存的个人资料。"),
 ("行动路线与明确采用", "在行动路线中，重新计算稳妥、均衡和冲刺组合。工作量是规划估计，不是实测效果。用户点击采用后，才会生成个人项目和事项。"),
 ("项目看板与时间线", "我的项目提供列表、看板、日历和时间线。赛事节点与执行任务共同呈现，材料清单可以持续推进。已有项目仍然占用后续规划的时间预算。"),
 ("来源变化与人工审核", "机会提醒显示来源扫描与变更状态。自动扫描需要另行配置；失败不会伪装成成功。变化必须经过管理员审核，本片不将模拟变化当成官方网站更新。"),
 ("可复核的交付边界", "当前离线测试和浏览器检查已通过，报告随源码提供。生产环境阻止弱密钥和调试管理员登录。线上旧凭据撤销尚未验证，真实用户效果也仍待验证。"),
]

def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True)
    items=[dict(title=t,text=s,audio=str((directory/f'{i:02}.wav').resolve())) for i,(t,s) in enumerate(CHAPTERS)]
    manifest=directory/'chapters.json'
    manifest.write_text(json.dumps(items,ensure_ascii=False),encoding='utf-8')
    ps=directory/'narrate.ps1'
    ps.write_text('''Add-Type -AssemblyName System.Speech
$segments = Get-Content -LiteralPath $env:CAMPUS_NARRATION_MANIFEST -Raw -Encoding UTF8 | ConvertFrom-Json
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice.SelectVoice('Microsoft Huihui Desktop')
foreach ($segment in $segments) {
  $voice.SetOutputToWaveFile($segment.audio)
  $voice.Speak($segment.text)
  $voice.SetOutputToNull()
}
$voice.Dispose()
''',encoding='utf-8-sig')
    subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ps)],check=True,env=dict(os.environ,CAMPUS_NARRATION_MANIFEST=str(manifest.resolve())))
    for item in items:
        with wave.open(item['audio'],'rb') as audio:
            item['duration']=max(22,int(audio.getnframes()/audio.getframerate())+3)
    manifest.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'docs/DEMO_NARRATION.md').write_text('# v1.3 实际浏览器演示旁白\n\n'+'\n\n'.join(f"{i+1}. **{v['title']}**（{v['duration']}秒）\n\n{v['text']}" for i,v in enumerate(items))+'\n',encoding='utf-8')
    print('Prepared local narration:',sum(v['duration'] for v in items),'seconds')

def assemble(directory,recording,output):
    items=json.loads((directory/'chapters.json').read_text(encoding='utf-8'))
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    clips=[]
    for i,item in enumerate(items):
        clip=directory/f'padded-{i}.wav'
        subprocess.run([ffmpeg,'-v','error','-y','-i',item['audio'],'-af','apad','-t',str(item['duration']),str(clip)],check=True)
        clips.append(clip)
    listing=directory/'audio.txt'
    listing.write_text('\n'.join("file '"+v.resolve().as_posix()+"'" for v in clips),encoding='utf-8')
    audio=directory/'narration.wav'
    subprocess.run([ffmpeg,'-v','error','-y','-f','concat','-safe','0','-i',str(listing),'-c:a','pcm_s16le',str(audio)],check=True)
    output.parent.mkdir(parents=True,exist_ok=True)
    # Keep native ffmpeg paths ASCII and bound decoder/encoder threads on large CPU hosts.
    encoded=directory/'encoded.mp4'
    subprocess.run([ffmpeg,'-v','error','-y','-threads','2','-i',str(recording),'-i',str(audio),'-map','0:v','-map','1:a','-vf','fps=25,format=yuv420p','-c:v','libx264','-threads','2','-preset','fast','-crf','23','-c:a','aac','-movflags','+faststart','-shortest',str(encoded)],check=True)
    shutil.copyfile(encoded,output)
    print('Actual browser recording assembled:',output)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work-dir',type=Path,default=ROOT/'tmp/demo-v13')
    p.add_argument('--prepare',action='store_true')
    p.add_argument('--recording',type=Path)
    p.add_argument('--output',type=Path,default=ROOT/'demo/演示视频.mp4')
    args=p.parse_args()
    if args.prepare: prepare(args.work_dir)
    else:
        if not args.recording: p.error('--recording required for assembly')
        assemble(args.work_dir,args.recording,args.output)
