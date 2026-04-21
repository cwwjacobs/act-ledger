from claude_act import ClaudeAct

client = ClaudeAct()

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=220,
    messages=[
        {
            "role": "user",
            "content": "Explain recursion in 3 short paragraphs and include one simple Python example."
        }
    ],
)

print(response.content[0].text)
