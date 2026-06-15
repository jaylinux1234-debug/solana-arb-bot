import phoenixpy  # pip install git+https://github.com/Ellipsis-Labs/phoenixpy.git

from src.config.settings import get_settings
from src.core.sizing import dynamic_flash_size
from src.dex.jupiter import JupiterExecutor


class MultiVenueCEX:
    def __init__(self):
        self.venues = {
            'backpack': self._init_backpack(),
            'phoenix': self._init_phoenix()
        }
        self.jupiter = JupiterExecutor()
        self.primary = 'backpack'

    def _init_backpack(self):
        # Your existing Backpack client
        from src.cex.backpack import BackpackClient
        return BackpackClient()

    def _init_phoenix(self):
        # Phoenix V1 on-chain orderbook
        settings = get_settings()
        rpc = settings.SOLANA_RPC_URL_FAST or settings.SOLANA_RPC_URL
        return phoenixpy.PhoenixClient(
            market_address="4DoNfFBfF7UokCC2FQzriy7yHK6DY6NVdYpuekQ5pRgg",  # SOL/USDC
            rpc_url=rpc,
        )

    async def get_best_price(self, side: str, amount_usdc: int) -> dict:
        """Compare Backpack vs Phoenix for best execution"""
        prices = {}
        
        # Backpack (CEX)
        bp_price = await self.venues['backpack'].get_price(side, amount_usdc)
        prices['backpack'] = {'price': bp_price, 'venue': 'backpack', 'type': 'cex'}
        
        # Phoenix V1 (On-chain)
        px_price = await self.venues['phoenix'].get_quote(side, amount_usdc)
        prices['phoenix'] = {'price': px_price, 'venue': 'phoenix', 'type': 'dex'}
        
        # Choose best
        best = max(prices.values(), key=lambda x: x['price'] if side == 'sell' else -x['price'])
        return best

    async def execute_hybrid_trade(self, signal: dict):
        size_usdc = dynamic_flash_size(
            base=150_000,
            utilization=0.68,
            volatility=signal.get('volatility_bps', 80)
        )
        
        # Get best venue
        buy_info = await self.get_best_price('buy', size_usdc)
        sell_info = await self.get_best_price('sell', size_usdc)
        
        if buy_info['venue'] == 'backpack':
            await self.venues['backpack'].place_order('buy', size_usdc)
            # Then withdraw → Jupiter
            await self.jupiter.swap_usdc_to_sol(size_usdc)
        else:
            # Pure on-chain Phoenix + Jupiter leg
            await self.venues['phoenix'].place_limit_order('buy', size_usdc)
        
        return {
            "venue": buy_info['venue'],
            "size_usdc": size_usdc,
            "expected_edge_bps": signal.get('gross_spread_bps', 0) - 45
        }