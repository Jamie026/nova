import pytest

from nova.state import AgentState, StateMachine


@pytest.mark.asyncio
async def test_camino_feliz_completo():
    machine = StateMachine()
    secuencia = [
        AgentState.WAKE_WORD_DETECTED, AgentState.LISTENING, AgentState.THINKING,
        AgentState.EXECUTING, AgentState.SPEAKING, AgentState.LISTENING,
        AgentState.SLEEPING, AgentState.IDLE,
    ]
    for target in secuencia:
        assert await machine.to(target), f"falló {target}"
    assert machine.state is AgentState.IDLE


@pytest.mark.asyncio
async def test_transicion_invalida_no_cambia_estado():
    machine = StateMachine(initial=AgentState.SLEEPING)
    assert not await machine.to(AgentState.SPEAKING)
    assert machine.state is AgentState.SLEEPING


@pytest.mark.asyncio
async def test_modo_estricto_lanza():
    machine = StateMachine(initial=AgentState.SLEEPING, strict=True)
    with pytest.raises(ValueError):
        await machine.to(AgentState.SPEAKING)


@pytest.mark.asyncio
async def test_notifica_a_los_listeners():
    machine = StateMachine()
    visto = []

    async def listener(previo, nuevo):
        visto.append((previo, nuevo))

    machine.on_change(listener)
    await machine.to(AgentState.LISTENING)
    assert visto == [(AgentState.IDLE, AgentState.LISTENING)]


@pytest.mark.asyncio
async def test_barge_in_speaking_a_listening():
    machine = StateMachine(initial=AgentState.SPEAKING)
    assert await machine.to(AgentState.LISTENING)
