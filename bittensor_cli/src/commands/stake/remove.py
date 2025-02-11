import asyncio
from functools import partial

from typing import TYPE_CHECKING, Optional
import typer

from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError
from rich.prompt import Confirm, Prompt
from rich.table import Table

from async_substrate_interface.errors import SubstrateRequestException
from bittensor_cli.src import COLOR_PALETTE
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.utils import (
    console,
    err_console,
    print_verbose,
    print_error,
    get_hotkey_wallets_for_wallet,
    is_valid_ss58_address,
    format_error_message,
    group_subnets,
)

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


# Commands
async def unstake(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    hotkey_ss58_address: str,
    all_hotkeys: bool,
    include_hotkeys: list[str],
    exclude_hotkeys: list[str],
    amount: float,
    prompt: bool,
    interactive: bool,
    netuid: Optional[int],
    safe_staking: bool,
    rate_tolerance: float,
    allow_partial_stake: bool,
):
    """Unstake from hotkey(s)."""
    unstake_all_from_hk = False
    with console.status(
        f"Retrieving subnet data & identities from {subtensor.network}...",
        spinner="earth",
    ):
        all_sn_dynamic_info_, ck_hk_identities, old_identities = await asyncio.gather(
            subtensor.all_subnets(),
            subtensor.fetch_coldkey_hotkey_identities(),
            subtensor.get_delegate_identities(),
        )
        all_sn_dynamic_info = {info.netuid: info for info in all_sn_dynamic_info_}

    if interactive:
        hotkeys_to_unstake_from, unstake_all_from_hk = await _unstake_selection(
            subtensor,
            wallet,
            all_sn_dynamic_info,
            ck_hk_identities,
            old_identities,
            netuid=netuid,
        )
        if unstake_all_from_hk:
            hotkey_to_unstake_all = hotkeys_to_unstake_from[0]
            unstake_all_alpha = Confirm.ask(
                "\nUnstake [blue]all alpha stakes[/blue] and stake back to [blue]root[/blue]? (No will unstake everything)",
                default=True,
            )
            return await unstake_all(
                wallet=wallet,
                subtensor=subtensor,
                hotkey_ss58_address=hotkey_to_unstake_all[1],
                unstake_all_alpha=unstake_all_alpha,
                prompt=prompt,
            )

        if not hotkeys_to_unstake_from:
            console.print("[red]No unstake operations to perform.[/red]")
            return False
        netuids = list({netuid for _, _, netuid in hotkeys_to_unstake_from})

    else:
        netuids = (
            [int(netuid)]
            if netuid is not None
            else await subtensor.get_all_subnet_netuids()
        )
        hotkeys_to_unstake_from = _get_hotkeys_to_unstake(
            wallet=wallet,
            hotkey_ss58_address=hotkey_ss58_address,
            all_hotkeys=all_hotkeys,
            include_hotkeys=include_hotkeys,
            exclude_hotkeys=exclude_hotkeys,
        )

    with console.status(
        f"Retrieving stake data from {subtensor.network}...",
        spinner="earth",
    ):
        # Fetch stake balances
        chain_head = await subtensor.substrate.get_chain_head()
        stake_info_list = await subtensor.get_stake_for_coldkey(
            coldkey_ss58=wallet.coldkeypub.ss58_address,
            block_hash=chain_head,
        )
        stake_in_netuids = {}
        for stake_info in stake_info_list:
            if stake_info.hotkey_ss58 not in stake_in_netuids:
                stake_in_netuids[stake_info.hotkey_ss58] = {}
            stake_in_netuids[stake_info.hotkey_ss58][stake_info.netuid] = (
                stake_info.stake
            )

    # Flag to check if user wants to quit
    skip_remaining_subnets = False
    if len(netuids) > 1 and not amount:
        console.print(
            "[dark_sea_green3]Tip: Enter 'q' any time to stop going over remaining subnets and process current unstakes.\n"
        )

    # Iterate over hotkeys and netuids to collect unstake operations
    unstake_operations = []
    total_received_amount = Balance.from_tao(0)
    max_float_slippage = 0
    table_rows = []
    for hotkey in hotkeys_to_unstake_from:
        if skip_remaining_subnets:
            break

        if interactive:
            staking_address_name, staking_address_ss58, netuid = hotkey
            netuids_to_process = [netuid]
        else:
            staking_address_name, staking_address_ss58 = hotkey
            netuids_to_process = netuids

        initial_amount = amount

        for netuid in netuids_to_process:
            if skip_remaining_subnets:
                break  # Exit the loop over netuids

            subnet_info = all_sn_dynamic_info.get(netuid)
            if staking_address_ss58 not in stake_in_netuids:
                print_error(
                    f"No stake found for hotkey: {staking_address_ss58} on netuid: {netuid}"
                )
                continue  # Skip to next hotkey

            current_stake_balance = stake_in_netuids[staking_address_ss58].get(netuid)
            if current_stake_balance is None or current_stake_balance.tao == 0:
                print_error(
                    f"No stake to unstake from {staking_address_ss58} on netuid: {netuid}"
                )
                continue  # No stake to unstake

            # Determine the amount we are unstaking.
            if initial_amount:
                amount_to_unstake_as_balance = Balance.from_tao(initial_amount)
            else:
                amount_to_unstake_as_balance = _ask_unstake_amount(
                    current_stake_balance,
                    netuid,
                    staking_address_name
                    if staking_address_name
                    else staking_address_ss58,
                    staking_address_ss58,
                    interactive,
                )
                if amount_to_unstake_as_balance is None:
                    skip_remaining_subnets = True
                    break

            # Check enough stake to remove.
            amount_to_unstake_as_balance.set_unit(netuid)
            if amount_to_unstake_as_balance > current_stake_balance:
                err_console.print(
                    f"[red]Not enough stake to remove[/red]:\n Stake balance: [dark_orange]{current_stake_balance}[/dark_orange]"
                    f" < Unstaking amount: [dark_orange]{amount_to_unstake_as_balance}[/dark_orange] on netuid: {netuid}"
                )
                continue  # Skip to the next subnet - useful when single amount is specified for all subnets

            received_amount, slippage_pct, slippage_pct_float = _calculate_slippage(
                subnet_info=subnet_info, amount=amount_to_unstake_as_balance
            )
            total_received_amount += received_amount
            max_float_slippage = max(max_float_slippage, slippage_pct_float)

            base_unstake_op = {
                "netuid": netuid,
                "hotkey_name": staking_address_name
                if staking_address_name
                else staking_address_ss58,
                "hotkey_ss58": staking_address_ss58,
                "amount_to_unstake": amount_to_unstake_as_balance,
                "current_stake_balance": current_stake_balance,
                "received_amount": received_amount,
                "slippage_pct": slippage_pct,
                "slippage_pct_float": slippage_pct_float,
                "dynamic_info": subnet_info,
            }

            base_table_row = [
                str(netuid),  # Netuid
                staking_address_name,  # Hotkey Name
                str(amount_to_unstake_as_balance),  # Amount to Unstake
                str(subnet_info.price.tao)
                + f"({Balance.get_unit(0)}/{Balance.get_unit(netuid)})",  # Rate
                str(received_amount),  # Received Amount
                slippage_pct,  # Slippage Percent
            ]

            # Additional fields for safe unstaking
            if safe_staking:
                if subnet_info.is_dynamic:
                    rate = subnet_info.price.tao or 1
                    rate_with_tolerance = rate * (
                        1 - rate_tolerance
                    )  # Rate only for display
                    price_with_tolerance = subnet_info.price.rao * (
                        1 - rate_tolerance
                    )  # Actual price to pass to extrinsic
                else:
                    rate_with_tolerance = 1
                    price_with_tolerance = 1

                base_unstake_op["price_with_tolerance"] = price_with_tolerance
                base_table_row.extend(
                    [
                        f"{rate_with_tolerance:.4f} {Balance.get_unit(0)}/{Balance.get_unit(netuid)}",  # Rate with tolerance
                        f"[{'dark_sea_green3' if allow_partial_stake else 'red'}]{allow_partial_stake}[/{'dark_sea_green3' if allow_partial_stake else 'red'}]",  # Partial unstake
                    ]
                )

            unstake_operations.append(base_unstake_op)
            table_rows.append(base_table_row)

    if not unstake_operations:
        console.print("[red]No unstake operations to perform.[/red]")
        return False

    table = _create_unstake_table(
        wallet_name=wallet.name,
        wallet_coldkey_ss58=wallet.coldkeypub.ss58_address,
        network=subtensor.network,
        total_received_amount=total_received_amount,
        safe_staking=safe_staking,
        rate_tolerance=rate_tolerance,
    )
    for row in table_rows:
        table.add_row(*row)

    _print_table_and_slippage(table, max_float_slippage, safe_staking)
    if prompt:
        if not Confirm.ask("Would you like to continue?"):
            raise typer.Exit()

    # Execute extrinsics
    try:
        wallet.unlock_coldkey()
    except KeyFileError:
        err_console.print("Error decrypting coldkey (possibly incorrect password)")
        return False

    with console.status("\n:satellite: Performing unstaking operations...") as status:
        if safe_staking:
            for op in unstake_operations:
                if op["netuid"] == 0:
                    await _unstake_extrinsic(
                        wallet=wallet,
                        subtensor=subtensor,
                        netuid=op["netuid"],
                        amount=op["amount_to_unstake"],
                        current_stake=op["current_stake_balance"],
                        hotkey_ss58=op["hotkey_ss58"],
                        status=status,
                    )
                else:
                    await _safe_unstake_extrinsic(
                        wallet=wallet,
                        subtensor=subtensor,
                        netuid=op["netuid"],
                        amount=op["amount_to_unstake"],
                        current_stake=op["current_stake_balance"],
                        hotkey_ss58=op["hotkey_ss58"],
                        price_limit=op["price_with_tolerance"],
                        allow_partial_stake=allow_partial_stake,
                        status=status,
                    )
        else:
            for op in unstake_operations:
                await _unstake_extrinsic(
                    wallet=wallet,
                    subtensor=subtensor,
                    netuid=op["netuid"],
                    amount=op["amount_to_unstake"],
                    current_stake=op["current_stake_balance"],
                    hotkey_ss58=op["hotkey_ss58"],
                    status=status,
                )
    console.print(
        f"[{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]Unstaking operations completed."
    )


async def unstake_all(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    hotkey_ss58_address: str,
    unstake_all_alpha: bool = False,
    prompt: bool = True,
) -> bool:
    """Unstakes all stakes from all hotkeys in all subnets."""

    with console.status(
        f"Retrieving stake information & identities from {subtensor.network}...",
        spinner="earth",
    ):
        (
            stake_info,
            ck_hk_identities,
            old_identities,
            all_sn_dynamic_info_,
            current_wallet_balance,
        ) = await asyncio.gather(
            subtensor.get_stake_for_coldkey(wallet.coldkeypub.ss58_address),
            subtensor.fetch_coldkey_hotkey_identities(),
            subtensor.get_delegate_identities(),
            subtensor.all_subnets(),
            subtensor.get_balance(wallet.coldkeypub.ss58_address),
        )
        if not hotkey_ss58_address:
            hotkey_ss58_address = wallet.hotkey.ss58_address
        stake_info = [
            stake for stake in stake_info if stake.hotkey_ss58 == hotkey_ss58_address
        ]

        if unstake_all_alpha:
            stake_info = [stake for stake in stake_info if stake.netuid != 0]

        if not stake_info:
            console.print("[red]No stakes found to unstake[/red]")
            return False

        all_sn_dynamic_info = {info.netuid: info for info in all_sn_dynamic_info_}

        # Create table for unstaking all
        table_title = (
            "Unstaking Summary - All Stakes"
            if not unstake_all_alpha
            else "Unstaking Summary - All Alpha Stakes"
        )
        table = Table(
            title=(
                f"\n[{COLOR_PALETTE['GENERAL']['HEADER']}]{table_title}[/{COLOR_PALETTE['GENERAL']['HEADER']}]\n"
                f"Wallet: [{COLOR_PALETTE['GENERAL']['COLDKEY']}]{wallet.name}[/{COLOR_PALETTE['GENERAL']['COLDKEY']}], "
                f"Coldkey ss58: [{COLOR_PALETTE['GENERAL']['COLDKEY']}]{wallet.coldkeypub.ss58_address}[/{COLOR_PALETTE['GENERAL']['COLDKEY']}]\n"
                f"Network: [{COLOR_PALETTE['GENERAL']['HEADER']}]{subtensor.network}[/{COLOR_PALETTE['GENERAL']['HEADER']}]\n"
            ),
            show_footer=True,
            show_edge=False,
            header_style="bold white",
            border_style="bright_black",
            style="bold",
            title_justify="center",
            show_lines=False,
            pad_edge=True,
        )
        table.add_column("Netuid", justify="center", style="grey89")
        table.add_column(
            "Hotkey", justify="center", style=COLOR_PALETTE["GENERAL"]["HOTKEY"]
        )
        table.add_column(
            f"Current Stake ({Balance.get_unit(1)})",
            justify="center",
            style=COLOR_PALETTE["STAKE"]["STAKE_ALPHA"],
        )
        table.add_column(
            f"Rate ({Balance.unit}/{Balance.get_unit(1)})",
            justify="center",
            style=COLOR_PALETTE["POOLS"]["RATE"],
        )
        table.add_column(
            f"Recieved ({Balance.unit})",
            justify="center",
            style=COLOR_PALETTE["POOLS"]["TAO_EQUIV"],
        )
        table.add_column(
            "Slippage",
            justify="center",
            style=COLOR_PALETTE["STAKE"]["SLIPPAGE_PERCENT"],
        )

        # Calculate slippage and total received
        max_slippage = 0.0
        total_received_value = Balance(0)
        for stake in stake_info:
            if stake.stake.rao == 0:
                continue

            # Get hotkey identity
            if hk_identity := ck_hk_identities["hotkeys"].get(stake.hotkey_ss58):
                hotkey_name = hk_identity.get("identity", {}).get(
                    "name", ""
                ) or hk_identity.get("display", "~")
                hotkey_display = f"{hotkey_name}"
            elif old_identity := old_identities.get(stake.hotkey_ss58):
                hotkey_name = old_identity.display
                hotkey_display = f"{hotkey_name}"
            else:
                hotkey_display = stake.hotkey_ss58

            subnet_info = all_sn_dynamic_info.get(stake.netuid)
            stake_amount = stake.stake
            received_amount, slippage_pct, slippage_pct_float = _calculate_slippage(
                subnet_info=subnet_info, amount=stake_amount
            )
            max_slippage = max(max_slippage, slippage_pct_float)
            total_received_value += received_amount

            table.add_row(
                str(stake.netuid),
                hotkey_display,
                str(stake_amount),
                str(float(subnet_info.price))
                + f"({Balance.get_unit(0)}/{Balance.get_unit(stake.netuid)})",
                str(received_amount),
                slippage_pct,
            )
    console.print(table)
    message = ""
    if max_slippage > 5:
        message += f"[{COLOR_PALETTE['STAKE']['SLIPPAGE_TEXT']}]-------------------------------------------------------------------------------------------------------------------\n"
        message += f"[bold]WARNING:[/bold] The slippage on one of your operations is high: [{COLOR_PALETTE['STAKE']['SLIPPAGE_PERCENT']}]{max_slippage:.4f}%[/{COLOR_PALETTE['STAKE']['SLIPPAGE_PERCENT']}], this may result in a loss of funds.\n"
        message += "-------------------------------------------------------------------------------------------------------------------\n"
        console.print(message)

    console.print(
        f"Expected return after slippage: [{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]{total_received_value}"
    )

    if prompt and not Confirm.ask(
        "\nDo you want to proceed with unstaking everything?"
    ):
        return False

    try:
        wallet.unlock_coldkey()
    except KeyFileError:
        err_console.print("Error decrypting coldkey (possibly incorrect password)")
        return False

    console_status = (
        ":satellite: Unstaking all Alpha stakes..."
        if unstake_all_alpha
        else ":satellite: Unstaking all stakes..."
    )
    previous_root_stake = await subtensor.get_stake(
        hotkey_ss58=hotkey_ss58_address,
        coldkey_ss58=wallet.coldkeypub.ss58_address,
        netuid=0,
    )
    with console.status(console_status):
        call_function = "unstake_all_alpha" if unstake_all_alpha else "unstake_all"
        call = await subtensor.substrate.compose_call(
            call_module="SubtensorModule",
            call_function=call_function,
            call_params={"hotkey": hotkey_ss58_address},
        )
        success, error_message = await subtensor.sign_and_send_extrinsic(
            call=call,
            wallet=wallet,
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )

        if success:
            success_message = (
                ":white_heavy_check_mark: [green]Successfully unstaked all stakes[/green]"
                if not unstake_all_alpha
                else ":white_heavy_check_mark: [green]Successfully unstaked all Alpha stakes[/green]"
            )
            console.print(success_message)
            new_balance = await subtensor.get_balance(wallet.coldkeypub.ss58_address)
            console.print(
                f"Balance:\n [blue]{current_wallet_balance}[/blue] :arrow_right: [{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]{new_balance}"
            )
            if unstake_all_alpha:
                root_stake = await subtensor.get_stake(
                    hotkey_ss58=hotkey_ss58_address,
                    coldkey_ss58=wallet.coldkeypub.ss58_address,
                    netuid=0,
                )
                console.print(
                    f"Root Stake:\n [blue]{previous_root_stake}[/blue] :arrow_right: [{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]{root_stake}"
                )
            return True
        else:
            err_console.print(
                f":cross_mark: [red]Failed to unstake[/red]: {error_message}"
            )
            return False


# Extrinsics
async def _unstake_extrinsic(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    netuid: int,
    amount: Balance,
    current_stake: Balance,
    hotkey_ss58: str,
    status=None,
) -> None:
    """Execute a standard unstake extrinsic.

    Args:
        netuid: The subnet ID
        amount: Amount to unstake
        current_stake: Current stake balance
        hotkey_ss58: Hotkey SS58 address
        wallet: Wallet instance
        subtensor: Subtensor interface
        status: Optional status for console updates
    """
    err_out = partial(print_error, status=status)
    failure_prelude = (
        f":cross_mark: [red]Failed[/red] to unstake {amount} on Netuid {netuid}"
    )

    if status:
        status.update(
            f"\n:satellite: Unstaking {amount} from {hotkey_ss58} on netuid: {netuid} ..."
        )

    current_balance = await subtensor.get_balance(wallet.coldkeypub.ss58_address)
    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="remove_stake",
        call_params={
            "hotkey": hotkey_ss58,
            "netuid": netuid,
            "amount_unstaked": amount.rao,
        },
    )
    extrinsic = await subtensor.substrate.create_signed_extrinsic(
        call=call, keypair=wallet.coldkey
    )

    try:
        response = await subtensor.substrate.submit_extrinsic(
            extrinsic, wait_for_inclusion=True, wait_for_finalization=False
        )
        await response.process_events()

        if not await response.is_success:
            err_out(
                f"{failure_prelude} with error: "
                f"{format_error_message(await response.error_message, subtensor.substrate)}"
            )
            return

        # Fetch latest balance and stake
        block_hash = await subtensor.substrate.get_chain_head()
        new_balance, new_stake = await asyncio.gather(
            subtensor.get_balance(wallet.coldkeypub.ss58_address, block_hash),
            subtensor.get_stake(
                hotkey_ss58=hotkey_ss58,
                coldkey_ss58=wallet.coldkeypub.ss58_address,
                netuid=netuid,
                block_hash=block_hash,
            ),
        )

        console.print(":white_heavy_check_mark: [green]Finalized[/green]")
        console.print(
            f"Balance:\n  [blue]{current_balance}[/blue] :arrow_right: [{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]{new_balance}"
        )
        console.print(
            f"Subnet: [{COLOR_PALETTE['GENERAL']['SUBHEADING']}]{netuid}[/{COLOR_PALETTE['GENERAL']['SUBHEADING']}]"
            f" Stake:\n  [blue]{current_stake}[/blue] :arrow_right: [{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]{new_stake}"
        )

    except Exception as e:
        err_out(f"{failure_prelude} with error: {str(e)}")


async def _safe_unstake_extrinsic(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    netuid: int,
    amount: Balance,
    current_stake: Balance,
    hotkey_ss58: str,
    price_limit: Balance,
    allow_partial_stake: bool,
    status=None,
) -> None:
    """Execute a safe unstake extrinsic with price limit.

    Args:
        netuid: The subnet ID
        amount: Amount to unstake
        current_stake: Current stake balance
        hotkey_ss58: Hotkey SS58 address
        price_limit: Maximum acceptable price
        wallet: Wallet instance
        subtensor: Subtensor interface
        allow_partial_stake: Whether to allow partial unstaking
        status: Optional status for console updates
    """
    err_out = partial(print_error, status=status)
    failure_prelude = (
        f":cross_mark: [red]Failed[/red] to unstake {amount} on Netuid {netuid}"
    )

    if status:
        status.update(
            f"\n:satellite: Unstaking {amount} from {hotkey_ss58} on netuid: {netuid} ..."
        )

    block_hash = await subtensor.substrate.get_chain_head()

    current_balance, next_nonce, current_stake = await asyncio.gather(
        subtensor.get_balance(wallet.coldkeypub.ss58_address, block_hash),
        subtensor.substrate.get_account_next_index(wallet.coldkeypub.ss58_address),
        subtensor.get_stake(
            hotkey_ss58=hotkey_ss58,
            coldkey_ss58=wallet.coldkeypub.ss58_address,
            netuid=netuid,
        ),
    )

    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="remove_stake_limit",
        call_params={
            "hotkey": hotkey_ss58,
            "netuid": netuid,
            "amount_unstaked": amount.rao,
            "limit_price": price_limit,
            "allow_partial": allow_partial_stake,
        },
    )

    extrinsic = await subtensor.substrate.create_signed_extrinsic(
        call=call, keypair=wallet.coldkey, nonce=next_nonce
    )

    try:
        response = await subtensor.substrate.submit_extrinsic(
            extrinsic, wait_for_inclusion=True, wait_for_finalization=False
        )
    except SubstrateRequestException as e:
        if "Custom error: 8" in str(e):
            print_error(
                f"\n{failure_prelude}: Price exceeded tolerance limit. "
                f"Transaction rejected because partial unstaking is disabled. "
                f"Either increase price tolerance or enable partial unstaking.",
                status=status,
            )
            return
        else:
            err_out(
                f"\n{failure_prelude} with error: {format_error_message(e, subtensor.substrate)}"
            )
        return

    await response.process_events()
    if not await response.is_success:
        err_out(
            f"\n{failure_prelude} with error: {format_error_message(await response.error_message, subtensor.substrate)}"
        )
        return

    block_hash = await subtensor.substrate.get_chain_head()
    new_balance, new_stake = await asyncio.gather(
        subtensor.get_balance(wallet.coldkeypub.ss58_address, block_hash),
        subtensor.get_stake(
            hotkey_ss58=hotkey_ss58,
            coldkey_ss58=wallet.coldkeypub.ss58_address,
            netuid=netuid,
            block_hash=block_hash,
        ),
    )

    console.print(":white_heavy_check_mark: [green]Finalized[/green]")
    console.print(
        f"Balance:\n  [blue]{current_balance}[/blue] :arrow_right: [{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]{new_balance}"
    )

    amount_unstaked = current_stake - new_stake
    if allow_partial_stake and (amount_unstaked != amount):
        console.print(
            "Partial unstake transaction. Unstaked:\n"
            f"  [{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]{amount_unstaked.set_unit(netuid=netuid)}[/{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}] "
            f"instead of "
            f"[blue]{amount}[/blue]"
        )

    console.print(
        f"Subnet: [{COLOR_PALETTE['GENERAL']['SUBHEADING']}]{netuid}[/{COLOR_PALETTE['GENERAL']['SUBHEADING']}] "
        f"Stake:\n  [blue]{current_stake}[/blue] :arrow_right: [{COLOR_PALETTE['STAKE']['STAKE_AMOUNT']}]{new_stake}"
    )


# Helpers
def _calculate_slippage(subnet_info, amount: Balance) -> tuple[Balance, str, float]:
    """Calculate slippage and received amount for unstaking operation.

    Args:
        dynamic_info: Subnet information containing price data
        amount: Amount being unstaked

    Returns:
        tuple containing:
        - received_amount: Balance after slippage
        - slippage_pct: Formatted string of slippage percentage
        - slippage_pct_float: Float value of slippage percentage
    """
    received_amount, _, slippage_pct_float = subnet_info.alpha_to_tao_with_slippage(
        amount
    )

    if subnet_info.is_dynamic:
        slippage_pct = f"{slippage_pct_float:.4f} %"
    else:
        slippage_pct_float = 0
        slippage_pct = "[red]N/A[/red]"

    return received_amount, slippage_pct, slippage_pct_float


async def _unstake_selection(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    dynamic_info,
    identities,
    old_identities,
    netuid: Optional[int] = None,
):
    stake_infos = await subtensor.get_stake_for_coldkey(
        coldkey_ss58=wallet.coldkeypub.ss58_address
    )

    if not stake_infos:
        print_error("You have no stakes to unstake.")
        raise typer.Exit()

    hotkey_stakes = {}
    for stake_info in stake_infos:
        if netuid is not None and stake_info.netuid != netuid:
            continue
        hotkey_ss58 = stake_info.hotkey_ss58
        netuid_ = stake_info.netuid
        stake_amount = stake_info.stake
        if stake_amount.tao > 0:
            hotkey_stakes.setdefault(hotkey_ss58, {})[netuid_] = stake_amount

    if not hotkey_stakes:
        if netuid is not None:
            print_error(f"You have no stakes to unstake in subnet {netuid}.")
        else:
            print_error("You have no stakes to unstake.")
        raise typer.Exit()

    hotkeys_info = []
    for idx, (hotkey_ss58, netuid_stakes) in enumerate(hotkey_stakes.items()):
        if hk_identity := identities["hotkeys"].get(hotkey_ss58):
            hotkey_name = hk_identity.get("identity", {}).get(
                "name", ""
            ) or hk_identity.get("display", "~")
        elif old_identity := old_identities.get(hotkey_ss58):
            hotkey_name = old_identity.display
        else:
            hotkey_name = "~"
        # TODO: Add wallet ids here.

        hotkeys_info.append(
            {
                "index": idx,
                "identity": hotkey_name,
                "netuids": list(netuid_stakes.keys()),
                "hotkey_ss58": hotkey_ss58,
            }
        )

    # Display existing hotkeys, id, and staked netuids.
    subnet_filter = f" for Subnet {netuid}" if netuid is not None else ""
    table = Table(
        title=f"\n[{COLOR_PALETTE['GENERAL']['HEADER']}]Hotkeys with Stakes{subnet_filter}\n",
        show_footer=True,
        show_edge=False,
        header_style="bold white",
        border_style="bright_black",
        style="bold",
        title_justify="center",
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("Index", justify="right")
    table.add_column("Identity", style=COLOR_PALETTE["GENERAL"]["SUBHEADING"])
    table.add_column("Netuids", style=COLOR_PALETTE["GENERAL"]["NETUID"])
    table.add_column("Hotkey Address", style=COLOR_PALETTE["GENERAL"]["HOTKEY"])

    for hotkey_info in hotkeys_info:
        index = str(hotkey_info["index"])
        identity = hotkey_info["identity"]
        netuids = group_subnets([n for n in hotkey_info["netuids"]])
        hotkey_ss58 = hotkey_info["hotkey_ss58"]
        table.add_row(index, identity, netuids, hotkey_ss58)

    console.print("\n", table)

    # Prompt to select hotkey to unstake.
    hotkey_options = [str(hotkey_info["index"]) for hotkey_info in hotkeys_info]
    hotkey_idx = Prompt.ask(
        "\nEnter the index of the hotkey you want to unstake from",
        choices=hotkey_options,
    )
    selected_hotkey_info = hotkeys_info[int(hotkey_idx)]
    selected_hotkey_ss58 = selected_hotkey_info["hotkey_ss58"]
    selected_hotkey_name = selected_hotkey_info["identity"]
    netuid_stakes = hotkey_stakes[selected_hotkey_ss58]

    # Display hotkey's staked netuids with amount.
    table = Table(
        title=f"\n[{COLOR_PALETTE['GENERAL']['HEADER']}]Stakes for hotkey \n[{COLOR_PALETTE['GENERAL']['SUBHEADING']}]{selected_hotkey_name}\n{selected_hotkey_ss58}\n",
        show_footer=True,
        show_edge=False,
        header_style="bold white",
        border_style="bright_black",
        style="bold",
        title_justify="center",
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("Subnet", justify="right")
    table.add_column("Symbol", style=COLOR_PALETTE["GENERAL"]["SYMBOL"])
    table.add_column("Stake Amount", style=COLOR_PALETTE["STAKE"]["STAKE_AMOUNT"])
    table.add_column(
        f"[bold white]RATE ({Balance.get_unit(0)}_in/{Balance.get_unit(1)}_in)",
        style=COLOR_PALETTE["POOLS"]["RATE"],
        justify="left",
    )

    for netuid_, stake_amount in netuid_stakes.items():
        symbol = dynamic_info[netuid_].symbol
        rate = f"{dynamic_info[netuid_].price.tao:.4f} τ/{symbol}"
        table.add_row(str(netuid_), symbol, str(stake_amount), rate)
    console.print("\n", table, "\n")

    # Ask which netuids to unstake from for the selected hotkey.
    unstake_all = False
    if netuid is not None:
        selected_netuids = [netuid]
    else:
        while True:
            netuid_input = Prompt.ask(
                "\nEnter the netuids of the [blue]subnets to unstake[/blue] from (comma-separated), or '[blue]all[/blue]' to unstake from all",
                default="all",
            )

            if netuid_input.lower() == "all":
                selected_netuids = list(netuid_stakes.keys())
                unstake_all = True
                break
            else:
                try:
                    netuid_list = [int(n.strip()) for n in netuid_input.split(",")]
                    invalid_netuids = [n for n in netuid_list if n not in netuid_stakes]
                    if invalid_netuids:
                        print_error(
                            f"The following netuids are invalid or not available: {', '.join(map(str, invalid_netuids))}. Please try again."
                        )
                    else:
                        selected_netuids = netuid_list
                        break
                except ValueError:
                    print_error(
                        "Please enter valid netuids (numbers), separated by commas, or 'all'."
                    )

    hotkeys_to_unstake_from = []
    for netuid_ in selected_netuids:
        hotkeys_to_unstake_from.append(
            (selected_hotkey_name, selected_hotkey_ss58, netuid_)
        )
    return hotkeys_to_unstake_from, unstake_all


def _ask_unstake_amount(
    current_stake_balance: Balance,
    netuid: int,
    staking_address_name: str,
    staking_address_ss58: str,
    interactive: bool,
) -> Optional[Balance]:
    """Prompt the user to decide the amount to unstake.

    Args:
        current_stake_balance: The current stake balance available to unstake
        netuid: The subnet ID
        staking_address_name: Display name of the staking address
        staking_address_ss58: SS58 address of the staking address
        interactive: Whether in interactive mode (affects default choice)

    Returns:
        Balance amount to unstake, or None if user chooses to quit
    """
    stake_color = COLOR_PALETTE["STAKE"]["STAKE_AMOUNT"]
    display_address = (
        staking_address_name if staking_address_name else staking_address_ss58
    )

    # First prompt: Ask if user wants to unstake all
    unstake_all_prompt = (
        f"Unstake all: [{stake_color}]{current_stake_balance}[/{stake_color}]"
        f" from [{stake_color}]{display_address}[/{stake_color}]"
        f" on netuid: [{stake_color}]{netuid}[/{stake_color}]? [y/n/q]"
    )

    while True:
        response = Prompt.ask(
            unstake_all_prompt,
            choices=["y", "n", "q"],
            default="n",
            show_choices=True,
        ).lower()

        if response == "q":
            return None
        if response == "y":
            return current_stake_balance
        if response != "n":
            console.print("[red]Invalid input. Please enter 'y', 'n', or 'q'.[/red]")
            continue

        amount_prompt = (
            f"Enter amount to unstake in [{stake_color}]{Balance.get_unit(netuid)}[/{stake_color}]"
            f" from subnet: [{stake_color}]{netuid}[/{stake_color}]"
            f" (Max: [{stake_color}]{current_stake_balance}[/{stake_color}])"
        )

        while True:
            amount_input = Prompt.ask(amount_prompt)
            if amount_input.lower() == "q":
                return None

            try:
                amount_value = float(amount_input)

                # Validate amount
                if amount_value <= 0:
                    console.print("[red]Amount must be greater than zero.[/red]")
                    continue

                amount_to_unstake = Balance.from_tao(amount_value)
                amount_to_unstake.set_unit(netuid)

                if amount_to_unstake > current_stake_balance:
                    console.print(
                        f"[red]Amount exceeds current stake balance of {current_stake_balance}.[/red]"
                    )
                    continue

                return amount_to_unstake

            except ValueError:
                console.print(
                    "[red]Invalid input. Please enter a numeric value or 'q' to quit.[/red]"
                )


def _get_hotkeys_to_unstake(
    wallet: Wallet,
    hotkey_ss58_address: Optional[str],
    all_hotkeys: bool,
    include_hotkeys: list[str],
    exclude_hotkeys: list[str],
) -> list[tuple[Optional[str], str]]:
    """Get list of hotkeys to unstake from based on input parameters.

    Args:
        wallet: The wallet to unstake from
        hotkey_ss58_address: Specific hotkey SS58 address to unstake from
        all_hotkeys: Whether to unstake from all hotkeys
        include_hotkeys: List of hotkey names/addresses to include
        exclude_hotkeys: List of hotkey names to exclude

    Returns:
        List of tuples containing (hotkey_name, hotkey_ss58) pairs to unstake from
    """
    if hotkey_ss58_address:
        print_verbose(f"Unstaking from ss58 ({hotkey_ss58_address})")
        return [(None, hotkey_ss58_address)]

    if all_hotkeys:
        print_verbose("Unstaking from all hotkeys")
        all_hotkeys_: list[Wallet] = get_hotkey_wallets_for_wallet(wallet=wallet)
        return [
            (wallet.hotkey_str, wallet.hotkey.ss58_address)
            for wallet in all_hotkeys_
            if wallet.hotkey_str not in exclude_hotkeys
        ]

    if include_hotkeys:
        print_verbose("Unstaking from included hotkeys")
        result = []
        for hotkey_identifier in include_hotkeys:
            if is_valid_ss58_address(hotkey_identifier):
                result.append((None, hotkey_identifier))
            else:
                wallet_ = Wallet(
                    name=wallet.name,
                    path=wallet.path,
                    hotkey=hotkey_identifier,
                )
                result.append((wallet_.hotkey_str, wallet_.hotkey.ss58_address))
        return result

    # Only cli.config.wallet.hotkey is specified
    print_verbose(
        f"Unstaking from wallet: ({wallet.name}) from hotkey: ({wallet.hotkey_str})"
    )
    assert wallet.hotkey is not None
    return [(wallet.hotkey_str, wallet.hotkey.ss58_address)]


def _create_unstake_table(
    wallet_name: str,
    wallet_coldkey_ss58: str,
    network: str,
    total_received_amount: Balance,
    safe_staking: bool,
    rate_tolerance: float,
) -> Table:
    """Create a table summarizing unstake operations.

    Args:
        wallet_name: Name of the wallet
        wallet_coldkey_ss58: Coldkey SS58 address
        network: Network name
        total_received_amount: Total amount to be received after unstaking

    Returns:
        Rich Table object configured for unstake summary
    """
    title = (
        f"\n[{COLOR_PALETTE['GENERAL']['HEADER']}]Unstaking to: \n"
        f"Wallet: [{COLOR_PALETTE['GENERAL']['COLDKEY']}]{wallet_name}[/{COLOR_PALETTE['GENERAL']['COLDKEY']}], "
        f"Coldkey ss58: [{COLOR_PALETTE['GENERAL']['COLDKEY']}]{wallet_coldkey_ss58}[/{COLOR_PALETTE['GENERAL']['COLDKEY']}]\n"
        f"Network: {network}[/{COLOR_PALETTE['GENERAL']['HEADER']}]\n"
    )
    table = Table(
        title=title,
        show_footer=True,
        show_edge=False,
        header_style="bold white",
        border_style="bright_black",
        style="bold",
        title_justify="center",
        show_lines=False,
        pad_edge=True,
    )

    table.add_column("Netuid", justify="center", style="grey89")
    table.add_column(
        "Hotkey", justify="center", style=COLOR_PALETTE["GENERAL"]["HOTKEY"]
    )
    table.add_column(
        f"Amount ({Balance.get_unit(1)})",
        justify="center",
        style=COLOR_PALETTE["POOLS"]["TAO"],
    )
    table.add_column(
        f"Rate ({Balance.get_unit(0)}/{Balance.get_unit(1)})",
        justify="center",
        style=COLOR_PALETTE["POOLS"]["RATE"],
    )
    table.add_column(
        f"Received ({Balance.get_unit(0)})",
        justify="center",
        style=COLOR_PALETTE["POOLS"]["TAO_EQUIV"],
        footer=str(total_received_amount),
    )
    table.add_column(
        "Slippage", justify="center", style=COLOR_PALETTE["STAKE"]["SLIPPAGE_PERCENT"]
    )
    if safe_staking:
        table.add_column(
            f"Rate with tolerance: [blue]({rate_tolerance*100}%)[/blue]",
            justify="center",
            style=COLOR_PALETTE["POOLS"]["RATE"],
        )
        table.add_column(
            "Partial unstake enabled",
            justify="center",
            style=COLOR_PALETTE["STAKE"]["SLIPPAGE_PERCENT"],
        )

    return table


def _print_table_and_slippage(
    table: Table,
    max_float_slippage: float,
    safe_staking: bool,
) -> None:
    """Print the unstake summary table and additional information.

    Args:
        table: The Rich table containing unstake details
        max_float_slippage: Maximum slippage percentage across all operations
    """
    console.print(table)

    if max_float_slippage > 5:
        console.print(
            "\n"
            f"[{COLOR_PALETTE['STAKE']['SLIPPAGE_TEXT']}]-------------------------------------------------------------------------------------------------------------------\n"
            f"[bold]WARNING:[/bold]  The slippage on one of your operations is high: [{COLOR_PALETTE['STAKE']['SLIPPAGE_PERCENT']}]{max_float_slippage} %[/{COLOR_PALETTE['STAKE']['SLIPPAGE_PERCENT']}],"
            " this may result in a loss of funds.\n"
            f"-------------------------------------------------------------------------------------------------------------------\n"
        )
    base_description = """
[bold white]Description[/bold white]:
The table displays information about the stake remove operation you are about to perform.
The columns are as follows:
    - [bold white]Netuid[/bold white]: The netuid of the subnet you are unstaking from.
    - [bold white]Hotkey[/bold white]: The ss58 address or identity of the hotkey you are unstaking from. 
    - [bold white]Amount to Unstake[/bold white]: The stake amount you are removing from this key.
    - [bold white]Rate[/bold white]: The rate of exchange between TAO and the subnet's stake.
    - [bold white]Received[/bold white]: The amount of free balance TAO you will receive on this subnet after slippage.
    - [bold white]Slippage[/bold white]: The slippage percentage of the unstake operation. (0% if the subnet is not dynamic i.e. root)."""

    safe_staking_description = """
    - [bold white]Rate Tolerance[/bold white]: Maximum acceptable alpha rate. If the rate reduces below this tolerance, the transaction will be limited or rejected.
    - [bold white]Partial unstaking[/bold white]: If True, allows unstaking up to the rate tolerance limit. If False, the entire transaction will fail if rate tolerance is exceeded."""

    console.print(base_description + (safe_staking_description if safe_staking else ""))
