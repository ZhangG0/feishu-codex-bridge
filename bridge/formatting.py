"""Feishu Markdown cards and fence-aware text segmentation."""
import re

FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})([^\n]*)$')


def markdown_card(text):
    return {'schema': '2.0', 'config': {'wide_screen_mode': True},
            'body': {'elements': [{'tag': 'markdown', 'content': text or '（无文本回复）'}]}}


def message_chunks(text, size=2500):
    if not text:
        return ['（无文本回复）']
    result, current = [], ''
    fence, opening = None, ''
    limit = max(256, size)
    def flush():
        nonlocal current
        if current:
            result.append(current + ('\n' + fence if fence else ''))
        current = opening + '\n' if fence else ''
    for line in text.splitlines(keepends=True):
        match = FENCE.match(line.rstrip('\r\n'))
        if len(line) > 128:
            match = None
        if match and (fence is None or (match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip())):
            if current and len(current) + len(line) > limit:
                flush()
            current += line
            if fence:
                fence, opening = None, ''
            else:
                fence, opening = match[1], line.rstrip('\r\n')
            continue
        if current and len(line) <= limit - len(opening) - 1 and len(current) + len(line) > limit:
            flush()
        while line:
            remaining = limit - len(current)
            if remaining <= 0:
                flush()
                remaining = limit - len(current)
            # Hard split oversized lines. Never drop content or add HTML/@ syntax.
            current += line[:remaining]
            line = line[remaining:]
            if line:
                flush()
    if current:
        result.append(current + ('\n' + fence if fence else ''))
    return result


def task_summary(prompt, limit=24):
    """First meaningful line, bounded and literal; no model invocation."""
    for line in prompt.splitlines():
        line = re.sub(r'^\s*(?:#{1,6}\s+|[-*+>]\s+|\d+[.)、]\s+)', '', line).strip()
        line = re.sub(r'\s+', ' ', line)
        if line and not re.fullmatch(r'[-_=*`~\s]+', line):
            return line if len(line) <= limit else line[:limit-1] + '…'
    return '文字任务'


def task_label(job):
    number = job.get('task_no')
    return '#%04d｜%s' % (number, task_summary(job.get('prompt', ''))) if number else task_summary(job.get('prompt', ''))
