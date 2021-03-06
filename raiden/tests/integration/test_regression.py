# -*- coding: utf-8 -*-
import gevent
import pytest

from raiden.messages import (
    Lock,
    LockedTransfer,
    RevealSecret,
    Secret,
)
from raiden.tests.fixtures.raiden_network import (
    CHAIN,
    wait_for_partners,
)
from raiden.tests.utils.network import setup_channels
from raiden.tests.utils.transfer import get_channelstate
from raiden.transfer.mediated_transfer.events import SendRevealSecret
from raiden.transfer.state import EMPTY_MERKLE_ROOT
from raiden.utils import sha3

# pylint: disable=too-many-locals


@pytest.mark.parametrize('number_of_nodes', [5])
@pytest.mark.parametrize('channels_per_node', [0])
@pytest.mark.parametrize('settle_timeout', [32])  # default settlement is too low for 3 hops
def test_regression_unfiltered_routes(raiden_network, token_addresses, settle_timeout, deposit):
    """ The transfer should proceed without triggering an assert.

    Transfers failed in networks where two or more paths to the destination are
    possible but they share same node as a first hop.
    """
    app0, app1, app2, app3, app4 = raiden_network
    token = token_addresses[0]

    # Topology:
    #
    #  0 -> 1 -> 2 -> 4
    #       |         ^
    #       +--> 3 ---+
    app_channels = [
        (app0, app1),
        (app1, app2),
        (app1, app3),
        (app3, app4),
        (app2, app4),
    ]

    setup_channels(
        token,
        app_channels,
        deposit,
        settle_timeout,
    )

    # poll the channel manager events
    wait_for_partners(raiden_network)

    transfer = app0.raiden.mediated_transfer_async(
        token_address=token,
        amount=1,
        target=app4.raiden.address,
        identifier=1,
    )
    assert transfer.wait()


@pytest.mark.parametrize('number_of_nodes', [3])
@pytest.mark.parametrize('channels_per_node', [CHAIN])
def test_regression_revealsecret_after_secret(raiden_network, token_addresses):
    """ A RevealSecret message received after a Secret message must be cleanly
    handled.
    """
    app0, app1, app2 = raiden_network
    token = token_addresses[0]

    identifier = 1
    transfer = app0.raiden.mediated_transfer_async(
        token_address=token,
        amount=1,
        target=app2.raiden.address,
        identifier=identifier,
    )
    assert transfer.wait()

    event = None
    for _, event in app1.raiden.wal.storage.get_events_by_block(0, 'latest'):
        if isinstance(event, SendRevealSecret):
            break
    assert event

    reveal_secret = RevealSecret(event.secret)
    app2.raiden.sign(reveal_secret)

    reveal_data = reveal_secret.encode()
    app1.raiden.protocol.receive(reveal_data)


@pytest.mark.parametrize('number_of_nodes', [2])
@pytest.mark.parametrize('channels_per_node', [CHAIN])
def test_regression_multiple_revealsecret(raiden_network, token_addresses):
    """ Multiple RevealSecret messages arriving at the same time must be
    handled properly.

    Secret handling followed these steps:

        The Secret message arrives
        The secret is registered
        The channel is updated and the correspoding lock is removed
        * A balance proof for the new channel state is created and sent to the
          payer
        The channel is unregistered for the given secrethash

    The step marked with an asterisk above introduced a context-switch. This
    allowed a second Reveal Secret message to be handled before the channel was
    unregistered. And because the channel was already updated an exception was raised
    for an unknown secret.
    """
    app0, app1 = raiden_network
    token = token_addresses[0]
    channelstate_0_1 = get_channelstate(app0, app1, token)

    identifier = 1
    secret = sha3(b'test_regression_multiple_revealsecret')
    secrethash = sha3(secret)
    expiration = app0.raiden.get_block_number() + 100
    amount = 10
    lock = Lock(
        amount,
        expiration,
        secrethash,
    )

    nonce = 1
    transferred_amount = 0
    mediated_transfer = LockedTransfer(
        identifier,
        nonce,
        token,
        channelstate_0_1.identifier,
        transferred_amount,
        app1.raiden.address,
        lock.secrethash,
        lock,
        app1.raiden.address,
        app0.raiden.address,
    )
    app0.raiden.sign(mediated_transfer)

    message_data = mediated_transfer.encode()
    app1.raiden.protocol.receive(message_data)

    reveal_secret = RevealSecret(secret)
    app0.raiden.sign(reveal_secret)
    reveal_secret_data = reveal_secret.encode()

    secret = Secret(
        identifier=identifier,
        nonce=mediated_transfer.nonce + 1,
        channel=channelstate_0_1.identifier,
        transferred_amount=amount,
        locksroot=EMPTY_MERKLE_ROOT,
        secret=secret,
    )
    app0.raiden.sign(secret)
    secret_data = secret.encode()

    messages = [
        secret_data,
        reveal_secret_data,
    ]

    wait = [
        gevent.spawn_later(
            .1,
            app1.raiden.protocol.receive,
            data,
        )
        for data in messages
    ]

    gevent.joinall(wait)
