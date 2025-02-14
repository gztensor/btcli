import asyncio

from typing import TYPE_CHECKING, Optional
import typer

from bittensor_wallet import Wallet
from rich.prompt import Prompt
from rich.table import Table
from rich import box
from rich.progress import Progress, BarColumn, TextColumn
from rich.console import Group
from rich.live import Live

from bittensor_cli.src import COLOR_PALETTE
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.chain_data import StakeInfo
from bittensor_cli.src.bittensor.utils import (
    console,
    print_error,
    millify_tao,
    get_subnet_name,
)

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


async def stake_list(
    wallet: Wallet,
    coldkey_ss58: str,
    subtensor: "SubtensorInterface",
    live: bool = False,
    verbose: bool = False,
    prompt: bool = False,
):
    coldkey_address = coldkey_ss58 if coldkey_ss58 else wallet.coldkeypub.ss58_address

    async def get_stake_data(block_hash: str = None):
        (
            sub_stakes,
            registered_delegate_info,
            _dynamic_info,
        ) = await asyncio.gather(
            subtensor.get_stake_for_coldkey(
                coldkey_ss58=coldkey_address, block_hash=block_hash
            ),
            subtensor.get_delegate_identities(block_hash=block_hash),
            subtensor.all_subnets(),
        )
        # sub_stakes = substakes[coldkey_address]
        dynamic_info = {info.netuid: info for info in _dynamic_info}
        return (
            sub_stakes,
            registered_delegate_info,
            dynamic_info,
        )

    def define_table(
        hotkey_name: str,
        rows: list[list[str]],
        total_tao_ownership: Balance,
        total_tao_value: Balance,
        total_swapped_tao_value: Balance,
        live: bool = False,
    ):
        title = f"\n[{COLOR_PALETTE['GENERAL']['HEADER']}]Hotkey: {hotkey_name}\nNetwork: {subtensor.network}\n\n"
        # TODO: Add hint back in after adding columns descriptions
        # if not live:
        #     title += f"[{COLOR_PALETTE['GENERAL']['HINT']}]See below for an explanation of the columns\n"
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
        table.add_column(
            "[white]Netuid",
            footer=f"{len(rows)}",
            footer_style="overline white",
            style="grey89",
        )
        table.add_column(
            "[white]Name",
            style="cyan",
            justify="left",
            no_wrap=True,
        )
        table.add_column(
            f"[white]Value \n({Balance.get_unit(1)} x {Balance.unit}/{Balance.get_unit(1)})",
            footer_style="overline white",
            style=COLOR_PALETTE["STAKE"]["TAO"],
            justify="right",
            footer=f"τ {millify_tao(total_tao_value.tao)}"
            if not verbose
            else f"{total_tao_value}",
        )
        table.add_column(
            f"[white]Stake ({Balance.get_unit(1)})",
            footer_style="overline white",
            style=COLOR_PALETTE["STAKE"]["STAKE_ALPHA"],
            justify="center",
        )
        table.add_column(
            f"[white]Price \n({Balance.unit}_in/{Balance.get_unit(1)}_in)",
            footer_style="white",
            style=COLOR_PALETTE["POOLS"]["RATE"],
            justify="center",
        )
        table.add_column(
            f"[white]Swap ({Balance.get_unit(1)} -> {Balance.unit})",
            footer_style="overline white",
            style=COLOR_PALETTE["STAKE"]["STAKE_SWAP"],
            justify="right",
            footer=f"τ {millify_tao(total_swapped_tao_value.tao)}"
            if not verbose
            else f"{total_swapped_tao_value}",
        )
        table.add_column(
            "[white]Registered",
            style=COLOR_PALETTE["STAKE"]["STAKE_ALPHA"],
            justify="right",
        )
        table.add_column(
            f"[white]Emission \n({Balance.get_unit(1)}/block)",
            style=COLOR_PALETTE["POOLS"]["EMISSION"],
            justify="right",
        )
        return table

    def create_table(hotkey_: str, substakes: list[StakeInfo]):
        name = (
            f"{registered_delegate_info[hotkey_].display} ({hotkey_})"
            if hotkey_ in registered_delegate_info
            else hotkey_
        )
        rows = []
        total_tao_ownership = Balance(0)
        total_tao_value = Balance(0)
        total_swapped_tao_value = Balance(0)
        root_stakes = [s for s in substakes if s.netuid == 0]
        other_stakes = sorted(
            [s for s in substakes if s.netuid != 0],
            key=lambda x: dynamic_info[x.netuid]
            .alpha_to_tao(Balance.from_rao(int(x.stake.rao)).set_unit(x.netuid))
            .tao,
            reverse=True,
        )
        sorted_substakes = root_stakes + other_stakes
        for substake_ in sorted_substakes:
            netuid = substake_.netuid
            pool = dynamic_info[netuid]
            symbol = f"{Balance.get_unit(netuid)}\u200e"
            # TODO: what is this price var for?
            price = (
                "{:.4f}{}".format(
                    pool.price.__float__(), f" τ/{Balance.get_unit(netuid)}\u200e"
                )
                if pool.is_dynamic
                else (f" 1.0000 τ/{symbol} ")
            )

            # Alpha value cell
            alpha_value = Balance.from_rao(int(substake_.stake.rao)).set_unit(netuid)

            # TAO value cell
            tao_value = pool.alpha_to_tao(alpha_value)
            total_tao_value += tao_value

            # Swapped TAO value and slippage cell
            swapped_tao_value, _, slippage_percentage_ = (
                pool.alpha_to_tao_with_slippage(substake_.stake)
            )
            total_swapped_tao_value += swapped_tao_value

            # Slippage percentage cell
            if pool.is_dynamic:
                slippage_percentage = f"[{COLOR_PALETTE['STAKE']['SLIPPAGE_PERCENT']}]{slippage_percentage_:.3f}%[/{COLOR_PALETTE['STAKE']['SLIPPAGE_PERCENT']}]"
            else:
                slippage_percentage = f"[{COLOR_PALETTE['STAKE']['SLIPPAGE_PERCENT']}]0.000%[/{COLOR_PALETTE['STAKE']['SLIPPAGE_PERCENT']}]"

            if netuid == 0:
                swap_value = f"[{COLOR_PALETTE['STAKE']['NOT_REGISTERED']}]N/A[/{COLOR_PALETTE['STAKE']['NOT_REGISTERED']}] ({slippage_percentage})"
            else:
                swap_value = (
                    f"τ {millify_tao(swapped_tao_value.tao)} ({slippage_percentage})"
                    if not verbose
                    else f"{swapped_tao_value} ({slippage_percentage})"
                )

            # TAO locked cell
            tao_locked = pool.tao_in

            # Issuance cell
            issuance = pool.alpha_out if pool.is_dynamic else tao_locked

            # Per block emission cell
            per_block_emission = substake_.emission.tao / (pool.tempo or 1)
            # Alpha ownership and TAO ownership cells
            if alpha_value.tao > 0.00009:
                if issuance.tao != 0:
                    # TODO figure out why this alpha_ownership does nothing
                    alpha_ownership = "{:.4f}".format(
                        (alpha_value.tao / issuance.tao) * 100
                    )
                    tao_ownership = Balance.from_tao(
                        (alpha_value.tao / issuance.tao) * tao_locked.tao
                    )
                    total_tao_ownership += tao_ownership
                else:
                    # TODO what's this var for?
                    alpha_ownership = "0.0000"
                    tao_ownership = Balance.from_tao(0)

                stake_value = (
                    millify_tao(substake_.stake.tao)
                    if not verbose
                    else f"{substake_.stake.tao:,.4f}"
                )
                subnet_name_cell = f"[{COLOR_PALETTE['GENERAL']['SYMBOL']}]{symbol if netuid != 0 else 'τ'}[/{COLOR_PALETTE['GENERAL']['SYMBOL']}] {get_subnet_name(dynamic_info[netuid])}"

                rows.append(
                    [
                        str(netuid),  # Number
                        subnet_name_cell,  # Symbol + name
                        f"τ {millify_tao(tao_value.tao)}"
                        if not verbose
                        else f"{tao_value}",  # Value (α x τ/α)
                        f"{stake_value} {symbol}"
                        if netuid != 0
                        else f"{symbol} {stake_value}",  # Stake (a)
                        f"{pool.price.tao:.4f} τ/{symbol}",  # Rate (t/a)
                        # f"τ {millify_tao(tao_ownership.tao)}" if not verbose else f"{tao_ownership}",  # TAO equiv
                        swap_value,  # Swap(α) -> τ
                        "YES"
                        if substake_.is_registered
                        else f"[{COLOR_PALETTE['STAKE']['NOT_REGISTERED']}]NO",  # Registered
                        str(Balance.from_tao(per_block_emission).set_unit(netuid)),
                        # Removing this flag for now, TODO: Confirm correct values are here w.r.t CHKs
                        # if substake_.is_registered
                        # else f"[{COLOR_PALETTE['STAKE']['NOT_REGISTERED']}]N/A",  # Emission(α/block)
                    ]
                )
        table = define_table(
            name, rows, total_tao_ownership, total_tao_value, total_swapped_tao_value
        )
        for row in rows:
            table.add_row(*row)
        console.print(table)
        return total_tao_ownership, total_tao_value

    def create_live_table(
        substakes: list,
        registered_delegate_info: dict,
        dynamic_info: dict,
        hotkey_name: str,
        previous_data: Optional[dict] = None,
    ) -> tuple[Table, dict, Balance, Balance, Balance]:
        rows = []
        current_data = {}

        total_tao_ownership = Balance(0)
        total_tao_value = Balance(0)
        total_swapped_tao_value = Balance(0)

        def format_cell(
            value, previous_value, unit="", unit_first=False, precision=4, millify=False
        ):
            if previous_value is not None:
                change = value - previous_value
                if abs(change) > 10 ** (-precision):
                    formatted_change = (
                        f"{change:.{precision}f}"
                        if not millify
                        else f"{millify_tao(change)}"
                    )
                    change_text = (
                        f" [pale_green3](+{formatted_change})[/pale_green3]"
                        if change > 0
                        else f" [hot_pink3]({formatted_change})[/hot_pink3]"
                    )
                else:
                    change_text = ""
            else:
                change_text = ""
            formatted_value = (
                f"{value:,.{precision}f}" if not millify else f"{millify_tao(value)}"
            )
            return (
                f"{formatted_value} {unit}{change_text}"
                if not unit_first
                else f"{unit} {formatted_value}{change_text}"
            )

        # Sort subnets by value
        root_stakes = [s for s in substakes if s.netuid == 0]
        other_stakes = sorted(
            [s for s in substakes if s.netuid != 0],
            key=lambda x: dynamic_info[x.netuid]
            .alpha_to_tao(Balance.from_rao(int(x.stake.rao)).set_unit(x.netuid))
            .tao,
            reverse=True,
        )
        sorted_substakes = root_stakes + other_stakes

        # Process each stake
        for substake in sorted_substakes:
            netuid = substake.netuid
            pool = dynamic_info.get(netuid)
            if substake.stake.rao == 0 or not pool:
                continue

            # Calculate base values
            symbol = f"{Balance.get_unit(netuid)}\u200e"
            alpha_value = Balance.from_rao(int(substake.stake.rao)).set_unit(netuid)
            tao_value = pool.alpha_to_tao(alpha_value)
            total_tao_value += tao_value
            swapped_tao_value, slippage, slippage_pct = pool.alpha_to_tao_with_slippage(
                substake.stake
            )
            total_swapped_tao_value += swapped_tao_value

            # Calculate TAO ownership
            tao_locked = pool.tao_in
            issuance = pool.alpha_out if pool.is_dynamic else tao_locked
            if alpha_value.tao > 0.00009 and issuance.tao != 0:
                tao_ownership = Balance.from_tao(
                    (alpha_value.tao / issuance.tao) * tao_locked.tao
                )
                total_tao_ownership += tao_ownership
            else:
                tao_ownership = Balance.from_tao(0)

            # Store current values for future delta tracking
            current_data[netuid] = {
                "stake": alpha_value.tao,
                "price": pool.price.tao,
                "tao_value": tao_value.tao,
                "swapped_value": swapped_tao_value.tao,
                "emission": substake.emission.tao / (pool.tempo or 1),
                "tao_ownership": tao_ownership.tao,
            }

            # Get previous values for delta tracking
            prev = previous_data.get(netuid, {}) if previous_data else {}
            unit_first = True if netuid == 0 else False

            stake_cell = format_cell(
                alpha_value.tao,
                prev.get("stake"),
                unit=symbol,
                unit_first=unit_first,
                precision=4,
                millify=True if not verbose else False,
            )

            rate_cell = format_cell(
                pool.price.tao,
                prev.get("price"),
                unit=f"τ/{symbol}",
                unit_first=False,
                precision=5,
                millify=True if not verbose else False,
            )

            exchange_cell = format_cell(
                tao_value.tao,
                prev.get("tao_value"),
                unit="τ",
                unit_first=True,
                precision=4,
                millify=True if not verbose else False,
            )

            if netuid != 0:
                swap_cell = (
                    format_cell(
                        swapped_tao_value.tao,
                        prev.get("swapped_value"),
                        unit="τ",
                        unit_first=True,
                        precision=4,
                        millify=True if not verbose else False,
                    )
                    + f" ({slippage_pct:.2f}%)"
                )
            else:
                swap_cell = f"[{COLOR_PALETTE['STAKE']['NOT_REGISTERED']}]N/A[/{COLOR_PALETTE['STAKE']['NOT_REGISTERED']}] ({slippage_pct}%)"

            emission_value = substake.emission.tao / (pool.tempo or 1)
            emission_cell = format_cell(
                emission_value,
                prev.get("emission"),
                unit=symbol,
                unit_first=unit_first,
                precision=4,
            )
            subnet_name_cell = (
                f"[{COLOR_PALETTE['GENERAL']['SYMBOL']}]{symbol if netuid != 0 else 'τ'}[/{COLOR_PALETTE['GENERAL']['SYMBOL']}]"
                f" {get_subnet_name(dynamic_info[netuid])}"
            )

            rows.append(
                [
                    str(netuid),  # Netuid
                    subnet_name_cell,
                    exchange_cell,  # Exchange value
                    stake_cell,  # Stake amount
                    rate_cell,  # Rate
                    swap_cell,  # Swap value with slippage
                    "YES"
                    if substake.is_registered
                    else f"[{COLOR_PALETTE['STAKE']['NOT_REGISTERED']}]NO",  # Registration status
                    emission_cell,  # Emission rate
                ]
            )

        table = define_table(
            hotkey_name,
            rows,
            total_tao_ownership,
            total_tao_value,
            total_swapped_tao_value,
            live=True,
        )

        for row in rows:
            table.add_row(*row)

        return table, current_data

    # Main execution
    (
        sub_stakes,
        registered_delegate_info,
        dynamic_info,
    ) = await get_stake_data()
    balance = await subtensor.get_balance(coldkey_address)

    # Iterate over substakes and aggregate them by hotkey.
    hotkeys_to_substakes: dict[str, list[StakeInfo]] = {}

    for substake in sub_stakes:
        hotkey = substake.hotkey_ss58
        if substake.stake.rao == 0:
            continue
        if hotkey not in hotkeys_to_substakes:
            hotkeys_to_substakes[hotkey] = []
        hotkeys_to_substakes[hotkey].append(substake)

    if not hotkeys_to_substakes:
        print_error(f"No stakes found for coldkey ss58: ({coldkey_address})")
        raise typer.Exit()

    if live:
        # Select one hokkey for live monitoring
        if len(hotkeys_to_substakes) > 1:
            console.print(
                "\n[bold]Multiple hotkeys found. Please select one for live monitoring:[/bold]"
            )
            for idx, hotkey in enumerate(hotkeys_to_substakes.keys()):
                name = (
                    f"{registered_delegate_info[hotkey].display} ({hotkey})"
                    if hotkey in registered_delegate_info
                    else hotkey
                )
                console.print(f"[{idx}] [{COLOR_PALETTE['GENERAL']['HEADER']}]{name}")

            selected_idx = Prompt.ask(
                "Enter hotkey index",
                choices=[str(i) for i in range(len(hotkeys_to_substakes))],
            )
            selected_hotkey = list(hotkeys_to_substakes.keys())[int(selected_idx)]
            selected_stakes = hotkeys_to_substakes[selected_hotkey]
        else:
            selected_hotkey = list(hotkeys_to_substakes.keys())[0]
            selected_stakes = hotkeys_to_substakes[selected_hotkey]

        hotkey_name = (
            f"{registered_delegate_info[selected_hotkey].display} ({selected_hotkey})"
            if selected_hotkey in registered_delegate_info
            else selected_hotkey
        )

        refresh_interval = 10  # seconds
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=20),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
        )
        progress_task = progress.add_task("Updating: ", total=refresh_interval)

        previous_block = None
        current_block = None
        previous_data = None

        with Live(console=console, screen=True, auto_refresh=True) as live:
            try:
                while True:
                    block_hash = await subtensor.substrate.get_chain_head()
                    (
                        sub_stakes,
                        registered_delegate_info,
                        dynamic_info_,
                    ) = await get_stake_data(block_hash)
                    selected_stakes = [
                        stake
                        for stake in sub_stakes
                        if stake.hotkey_ss58 == selected_hotkey
                    ]

                    block_number = await subtensor.substrate.get_block_number(None)

                    previous_block = current_block
                    current_block = block_number
                    new_blocks = (
                        "N/A"
                        if previous_block is None
                        else str(current_block - previous_block)
                    )

                    table, current_data = create_live_table(
                        selected_stakes,
                        registered_delegate_info,
                        dynamic_info,
                        hotkey_name,
                        previous_data,
                    )

                    previous_data = current_data
                    progress.reset(progress_task)
                    start_time = asyncio.get_event_loop().time()

                    block_info = (
                        f"Previous: [dark_sea_green]{previous_block}[/dark_sea_green] "
                        f"Current: [dark_sea_green]{current_block}[/dark_sea_green] "
                        f"Diff: [dark_sea_green]{new_blocks}[/dark_sea_green]"
                    )

                    message = f"\nLive stake view - Press [bold red]Ctrl+C[/bold red] to exit\n{block_info}"
                    live_render = Group(message, progress, table)
                    live.update(live_render)

                    while not progress.finished:
                        await asyncio.sleep(0.1)
                        elapsed = asyncio.get_event_loop().time() - start_time
                        progress.update(
                            progress_task, completed=min(elapsed, refresh_interval)
                        )

            except KeyboardInterrupt:
                console.print("\n[bold]Stopped live updates[/bold]")
                return

    else:
        # Iterate over each hotkey and make a table
        counter = 0
        num_hotkeys = len(hotkeys_to_substakes)
        all_hotkeys_total_global_tao = Balance(0)
        all_hotkeys_total_tao_value = Balance(0)
        for hotkey in hotkeys_to_substakes.keys():
            counter += 1
            stake, value = create_table(hotkey, hotkeys_to_substakes[hotkey])
            all_hotkeys_total_global_tao += stake
            all_hotkeys_total_tao_value += value

            if num_hotkeys > 1 and counter < num_hotkeys and prompt:
                console.print("\nPress Enter to continue to the next hotkey...")
                input()

        total_tao_value = (
            f"τ {millify_tao(all_hotkeys_total_tao_value.tao)}"
            if not verbose
            else all_hotkeys_total_tao_value
        )
        total_tao_ownership = (
            f"τ {millify_tao(all_hotkeys_total_global_tao.tao)}"
            if not verbose
            else all_hotkeys_total_global_tao
        )

        console.print("\n\n")
        console.print(
            f"Wallet:\n"
            f"  Coldkey SS58: [{COLOR_PALETTE['GENERAL']['COLDKEY']}]{coldkey_address}[/{COLOR_PALETTE['GENERAL']['COLDKEY']}]\n"
            f"  Free Balance: [{COLOR_PALETTE['GENERAL']['BALANCE']}]{balance}[/{COLOR_PALETTE['GENERAL']['BALANCE']}]\n"
            f"  Total TAO ({Balance.unit}): [{COLOR_PALETTE['GENERAL']['BALANCE']}]{total_tao_ownership}[/{COLOR_PALETTE['GENERAL']['BALANCE']}]\n"
            f"  Total Value ({Balance.unit}): [{COLOR_PALETTE['GENERAL']['BALANCE']}]{total_tao_value}[/{COLOR_PALETTE['GENERAL']['BALANCE']}]"
        )
        if not sub_stakes:
            console.print(
                f"\n[blue]No stakes found for coldkey ss58: ({coldkey_address})"
            )
        else:
            # TODO: Temporarily returning till we update docs
            return
            display_table = Prompt.ask(
                "\nPress Enter to view column descriptions or type 'q' to skip:",
                choices=["", "q"],
                default="",
                show_choices=True,
            ).lower()

            if display_table == "q":
                console.print(
                    f"[{COLOR_PALETTE['GENERAL']['SUBHEADING_EXTRA_1']}]Column descriptions skipped."
                )
            else:
                header = """
            [bold white]Description[/bold white]: Each table displays information about stake associated with a hotkey. The columns are as follows:
            """
                console.print(header)
                description_table = Table(
                    show_header=False, box=box.SIMPLE, show_edge=False, show_lines=True
                )

                fields = [
                    ("[bold tan]Netuid[/bold tan]", "The netuid of the subnet."),
                    (
                        "[bold tan]Symbol[/bold tan]",
                        "The symbol for the subnet's dynamic TAO token.",
                    ),
                    (
                        "[bold tan]Stake (α)[/bold tan]",
                        "The stake amount this hotkey holds in the subnet, expressed in subnet's alpha token currency. This can change whenever staking or unstaking occurs on this hotkey in this subnet. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#staking[/blue].",
                    ),
                    (
                        "[bold tan]TAO Reserves (τ_in)[/bold tan]",
                        'Number of TAO in the TAO reserves of the pool for this subnet. Attached to every subnet is a subnet pool, containing a TAO reserve and the alpha reserve. See also "Alpha Pool (α_in)" description. This can change every block. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#subnet-pool[/blue].',
                    ),
                    (
                        "[bold tan]Alpha Reserves (α_in)[/bold tan]",
                        "Number of subnet alpha tokens in the alpha reserves of the pool for this subnet. This reserve, together with 'TAO Pool (τ_in)', form the subnet pool for every subnet. This can change every block. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#subnet-pool[/blue].",
                    ),
                    (
                        "[bold tan]RATE (τ_in/α_in)[/bold tan]",
                        "Exchange rate between TAO and subnet dTAO token. Calculated as the reserve ratio: (TAO Pool (τ_in) / Alpha Pool (α_in)). Note that the terms relative price, alpha token price, alpha price are the same as exchange rate. This rate can change every block. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#rate-%CF%84_in%CE%B1_in[/blue].",
                    ),
                    (
                        "[bold tan]Alpha out (α_out)[/bold tan]",
                        "Total stake in the subnet, expressed in subnet's alpha token currency. This is the sum of all the stakes present in all the hotkeys in this subnet. This can change every block. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#stake-%CE%B1_out-or-alpha-out-%CE%B1_out",
                    ),
                    (
                        "[bold tan]TAO Equiv (τ_in x α/α_out)[/bold tan]",
                        'TAO-equivalent value of the hotkeys stake α (i.e., Stake(α)). Calculated as (TAO Reserves(τ_in) x (Stake(α) / ALPHA Out(α_out)). This value is weighted with (1-γ), where γ is the local weight coefficient, and used in determining the overall stake weight of the hotkey in this subnet. Also see the "Local weight coeff (γ)" column of "btcli subnet list" command output. This can change every block. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#local-weight-or-tao-equiv-%CF%84_in-x-%CE%B1%CE%B1_out[/blue].',
                    ),
                    (
                        "[bold tan]Exchange Value (α x τ/α)[/bold tan]",
                        "This is the potential τ you will receive, without considering slippage, if you unstake from this hotkey now on this subnet. See Swap(α → τ) column description. Note: The TAO Equiv(τ_in x α/α_out) indicates validator stake weight while this Exchange Value shows τ you will receive if you unstake now. This can change every block. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#exchange-value-%CE%B1-x-%CF%84%CE%B1[/blue].",
                    ),
                    (
                        "[bold tan]Swap (α → τ)[/bold tan]",
                        "This is the actual τ you will receive, after factoring in the slippage charge, if you unstake from this hotkey now on this subnet. The slippage is calculated as 1 - (Swap(α → τ)/Exchange Value(α x τ/α)), and is displayed in brackets. This can change every block. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#swap-%CE%B1--%CF%84[/blue].",
                    ),
                    (
                        "[bold tan]Registered[/bold tan]",
                        "Indicates if the hotkey is registered in this subnet or not. \nFor more, see [blue]https://docs.bittensor.com/learn/anatomy-of-incentive-mechanism#tempo[/blue].",
                    ),
                    (
                        "[bold tan]Emission (α/block)[/bold tan]",
                        "Shows the portion of the one α/block emission into this subnet that is received by this hotkey, according to YC2 in this subnet. This can change every block. \nFor more, see [blue]https://docs.bittensor.com/dynamic-tao/dtao-guide#emissions[/blue].",
                    ),
                ]

                description_table.add_column(
                    "Field",
                    no_wrap=True,
                    style="bold tan",
                )
                description_table.add_column("Description", overflow="fold")
                for field_name, description in fields:
                    description_table.add_row(field_name, description)
                console.print(description_table)
