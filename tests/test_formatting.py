import unittest
from bridge.formatting import markdown_card, message_chunks

class FormattingTests(unittest.TestCase):
    def test_markdown_card_preserves_syntax_and_has_no_approval_buttons(self):
        text='# 标题\n**重点**\n- 项目\n\n|A|B|\n|---|---|\n|1|2|\n\n```python\nprint(1)\n```'
        c=markdown_card(text)
        self.assertEqual(c['schema'],'2.0')
        self.assertEqual(c['body']['elements'],[{'tag':'markdown','content':text}])

    def test_code_fences_balanced_across_messages(self):
        text='说明\n```python\n'+''.join('print(%d)\n'%n for n in range(600))+'```\n完成'
        chunks=message_chunks(text)
        self.assertGreater(len(chunks),1)
        for chunk in chunks:self.assertEqual(sum(line.startswith('```') for line in chunk.splitlines())%2,0)
        for n in range(600):self.assertIn('print(%d)'%n,'\n'.join(chunks))
        self.assertTrue(chunks[-1].endswith('完成'))

    def test_plaintext_keeps_all_characters(self):
        text='中'*6001
        self.assertEqual(''.join(message_chunks(text)),text)
        self.assertEqual([len(c) for c in message_chunks(text)],[2500,2500,1001])

    def test_unclosed_fence_is_closed_and_tilde_supported(self):
        self.assertTrue(message_chunks('~~~sh\necho hello')[0].endswith('~~~'))
        self.assertEqual(message_chunks(''),['（无文本回复）'])

    def test_pathological_fence_does_not_loop(self):
        chunks=message_chunks('`'*10000+'\nabc')
        self.assertIn('abc',''.join(chunks))

if __name__=='__main__':unittest.main()
