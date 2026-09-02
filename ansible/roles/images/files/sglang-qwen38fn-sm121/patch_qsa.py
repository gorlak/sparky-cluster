"""Apply the SM121 QSA fix to SGLang inside the derived image (ADR-0030).

Reproduces sgl-project/sglang#36806 + #36845 — the root-cause fix MiaAI-Lab committed —
on top of the stock `lmsysorg/sglang:qwen38flashnext` base. On GB10 (SM121) the QSA decode
path must NOT take FlashInfer's TRT-LLM sparse-decode kernel (it silently emits token id 0
at long context, sglang#36537) and must NOT fall to FA4's CuTe varlen (it fails to compile
for this call shape) — it takes the Triton packed-varlen fallback in qsa/sm121_varlen.py,
copied in alongside.

Two guarded, idempotent source inserts. The asserts fail the BUILD loudly (a deploy, not a
20-minute weight load) if the base image's file layout ever shifts under us. This is a
clean standalone script on purpose: MiaAI-Lab applies the same edits via a shell heredoc
nested inside another heredoc, whose backslash-escaping does not survive being copied out.
"""
p = "/sgl-workspace/sglang/python/sglang/srt/layers/attention/qwen_sparse_attn_backend.py"
s = open(p).read()

# sglang#36845: SM121 takes the packed-varlen Triton fallback, not FA4 CuTe.
if "qsa.sm121_varlen" not in s:
    anchor = "    try:\n        from flash_attn import flash_attn_varlen_func"
    assert anchor in s, "flash_attn varlen anchor not found — upstream image layout changed"
    insert = (
        "    from sglang.srt.utils import is_sm121\n"
        "\n"
        "    if is_sm121():\n"
        "        from sglang.srt.layers.attention.qsa.sm121_varlen import (\n"
        "            qsa_sm121_varlen_attention,\n"
        "        )\n"
        "\n"
        "        return qsa_sm121_varlen_attention\n"
    )
    s = s.replace(anchor, insert + anchor, 1)

# sglang#36806: never take FlashInfer TRT-LLM sparse decode on SM121. A newer base image
# may re-enable it (the token-id-0-at-long-context regression), so this guard is idempotent
# and re-asserts on every rebuild.
marker = "dspark: SM121 must not use TRT-LLM sparse decode"
if marker not in s:
    fn = "def _resolve_trtllm_sparse_decode():"
    i = s.find(fn)
    assert i >= 0, "trtllm resolver not found — upstream image layout changed"
    ds = s.find('"""', i)
    assert ds > 0, "trtllm resolver docstring not found"
    ds_end = s.find('"""', ds + 3)
    assert ds_end > 0, "trtllm resolver docstring unterminated"
    ds_end += 3
    s = s[:ds_end] + (
        "\n    from sglang.srt.utils import is_sm121\n"
        "\n"
        "    # dspark: SM121 must not use TRT-LLM sparse decode\n"
        "    # (sglang#36806 / #36845). That path silently emits token id 0\n"
        "    # at long context on GB10.\n"
        "    if is_sm121():\n"
        "        return None\n"
    ) + s[ds_end:]

open(p, "w").write(s)
print("qwen_sparse_attn_backend.py patched for SM121 (sglang#36806 + #36845)")
