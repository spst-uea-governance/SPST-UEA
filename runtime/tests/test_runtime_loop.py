from spst_runtime.runtime.runtime_loop import RuntimeLoop

def test_loop():
    rt=RuntimeLoop()
    rt.start()
    rt.step()
    assert rt.ctx.tick==1
