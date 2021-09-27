import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, DefaultDict, Dict, List, NamedTuple, Optional, Tuple

from gevent.lock import Semaphore
from pysqlcipher3 import dbapi2 as sqlcipher

from rotkehlchen.assets.asset import Asset
from rotkehlchen.constants.assets import A_USD
from rotkehlchen.constants.misc import NFT_DIRECTIVE, ZERO
from rotkehlchen.errors import InputError, RemoteError, UnknownAsset
from rotkehlchen.externalapis.opensea import NFT, Opensea
from rotkehlchen.fval import FVal
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.typing import ChecksumEthAddress, Price
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.mixins.cacheable import CacheableMixIn, cache_response_timewise
from rotkehlchen.utils.mixins.lockable import LockableQueryMixIn, protect_with_lock

if TYPE_CHECKING:
    from rotkehlchen.accounting.structures import AssetBalance
    from rotkehlchen.chain.ethereum.manager import EthereumManager
    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.premium.premium import Premium

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)

FREE_NFT_LIMIT = 5


class NFTResult(NamedTuple):
    addresses: Dict[ChecksumEthAddress, List[NFT]]
    entries_found: int
    entries_limit: int

    def serialize(self) -> Dict[str, Any]:
        return {
            'addresses': {address: [x.serialize() for x in nfts] for address, nfts in self.addresses.items()},  # noqa: E501
            'entries_found': self.entries_found,
            'entries_limit': self.entries_limit,
        }


class Nfts(CacheableMixIn, LockableQueryMixIn):  # lgtm [py/missing-call-to-init]

    def __init__(
            self,
            ethereum_manager: 'EthereumManager',
            database: 'DBHandler',
            premium: Optional['Premium'],
            msg_aggregator: MessagesAggregator,
    ) -> None:
        super().__init__()
        self.msg_aggregator = msg_aggregator
        self.db = database
        self.ethereum = ethereum_manager
        self.premium = premium
        self.opensea = Opensea(database=database, msg_aggregator=msg_aggregator)
        self._query_lock = Semaphore()

    @protect_with_lock()
    @cache_response_timewise()
    def _get_all_nft_data(
            self,  # pylint: disable=unused-argument
            addresses: List[ChecksumEthAddress],
            # Kwargs here is so linters don't complain when the "magic" ignore_cache kwarg is given
            **kwargs: Any,
    ) -> Tuple[Dict[ChecksumEthAddress, List[NFT]], int]:
        result = {}
        total_nfts_num = 0
        for address in addresses:
            nfts = self.opensea.get_account_nfts(address)
            nfts_num = len(nfts)
            if nfts_num != 0:
                if self.premium is None:
                    if nfts_num + total_nfts_num > FREE_NFT_LIMIT:
                        remaining_size = FREE_NFT_LIMIT - total_nfts_num
                    else:
                        remaining_size = nfts_num
                    if remaining_size != 0:
                        result[address] = nfts[:remaining_size]
                        total_nfts_num += remaining_size

                    break  # we hit the nft limit

                result[address] = nfts
                total_nfts_num += nfts_num

        return result, total_nfts_num

    def get_all_info(
            self,
            addresses: List[ChecksumEthAddress],
            ignore_cache: bool,
    ) -> NFTResult:
        """Gets info for all NFTs of the given addresses

        Returns a tuple with:
        - Mapping of addresses to list of NFTs
        - Total NFTs found - integer
        - Limit for free NFTs - integer

        May raise:
        - RemoteError
        """
        result, total_nfts_num = self._get_all_nft_data(addresses, ignore_cache=ignore_cache)
        return NFTResult(
            addresses=result,
            entries_found=total_nfts_num,
            entries_limit=FREE_NFT_LIMIT,
        )

    def get_balances(
            self,
            addresses: List[ChecksumEthAddress],
            return_zero_values: bool,
            ignore_cache: bool,
    ) -> Dict[ChecksumEthAddress, List[Dict[str, Any]]]:
        result: DefaultDict[ChecksumEthAddress, List[Dict[str, Any]]] = defaultdict(list)
        nft_results, _ = self._get_all_nft_data(addresses, ignore_cache=ignore_cache)
        cached_db_result = self.get_nfts_with_price()
        cached_db_prices = {x['asset']: x for x in cached_db_result}
        db_data = []
        for address, nfts in nft_results.items():
            for nft in nfts:
                identifier = NFT_DIRECTIVE + nft.token_identifier
                cached_price_data = cached_db_prices.get(identifier)
                if cached_price_data is not None and cached_price_data['manually_input']:
                    result[address].append({
                        'id': identifier,
                        'name': nft.name,
                        'manually_input': True,
                        'price_asset': cached_price_data['price_asset'],
                        'price_in_asset': FVal(cached_price_data['price_in_asset']),
                        'usd_price': FVal(cached_price_data['usd_price']),
                    })
                elif nft.price_usd != ZERO:
                    usd_price = nft.price_usd
                    result[address].append({
                        'id': identifier,
                        'name': nft.name,
                        'manually_input': False,
                        'price_asset': 'USD',
                        'price_in_asset': usd_price,
                        'usd_price': usd_price,
                    })
                    db_data.append((identifier, nft.name, str(usd_price), 'USD', 0))
                else:
                    if return_zero_values:
                        result[address].append({
                            'id': identifier,
                            'name': nft.name,
                            'manually_input': False,
                            'price_asset': 'USD',
                            'price_in_asset': ZERO,
                            'usd_price': ZERO,
                        })

        # save opensea data in the DB
        if len(db_data) != 0:
            cursor = self.db.conn.cursor()
            cursor.executemany(
                'INSERT OR IGNORE INTO assets(identifier) VALUES(?)',
                [(x[0],) for x in db_data],
            )
            for entry in db_data:
                exist_result = cursor.execute(
                    'SELECT manual_price FROM nfts WHERE identifier=?',
                    (entry[0],),
                ).fetchone()
                if exist_result is None or bool(exist_result[0]) is False:
                    cursor.execute(
                        'INSERT OR IGNORE INTO nfts('
                        'identifier, name, last_price, last_price_asset, manual_price'
                        ') VALUES(?, ?, ?, ?, ?)',
                        entry,
                    )

        return result

    def get_nfts_with_price(self) -> List[Dict[str, Any]]:
        cursor = self.db.conn.cursor()
        query = cursor.execute('SELECT identifier, last_price, last_price_asset, manual_price from nfts WHERE last_price IS NOT NULL')  # noqa: E501
        result = []
        for entry in query:
            to_asset_id = entry[2] if entry[2] is not None else A_USD
            try:
                to_asset = Asset(to_asset_id)
            except UnknownAsset:
                log.error(f'Unknown asset {to_asset_id} in custom nft price DB table. Ignoring.')
                continue

            if to_asset != A_USD:
                try:
                    to_asset_usd_price = Inquirer().find_usd_price(to_asset)
                except RemoteError as e:
                    log.error(
                        f'Error querying current usd price of {to_asset} in custom nft price '
                        f'api call due to {str(e)}. Ignoring.',
                    )
                    continue
                if to_asset_usd_price == ZERO:
                    log.error(
                        f'Could not find current usd price for {to_asset} in custom nft '
                        f'price api call. Ignoring.',
                    )
                    continue
                usd_price = to_asset_usd_price * FVal(entry[1])
            else:  # to_asset == USD
                usd_price = entry[1]

            result.append({
                'asset': entry[0],
                'manually_input': bool(entry[3]),
                'price_asset': to_asset_id,
                'price_in_asset': entry[1],
                'usd_price': str(usd_price),
            })

        return result

    def add_nft_with_price(
            self,
            from_asset: Asset,
            to_asset: Asset,
            price: Price,
    ) -> bool:
        """May raise:
         - InputError
        """
        cursor = self.db.conn.cursor()
        try:
            cursor.execute(
                'UPDATE nfts SET last_price=?, last_price_asset=?, manual_price=? '
                'WHERE identifier=?',
                (str(price), to_asset.identifier, 1, from_asset.identifier),
            )
        except sqlcipher.DatabaseError as e:  # pylint: disable=no-member
            raise InputError(f'Failed to write price for {from_asset.identifier} due to {str(e)}') from e  # noqa: E501

        if cursor.rowcount == 1:
            return True  # all done

        # else it means no DB entry existed, so we need to make a full entry.
        cursor.execute(
            'INSERT OR IGNORE INTO assets(identifier) VALUES(?)',
            (from_asset.identifier,),
        )
        cursor.execute(
            'INSERT OR IGNORE INTO nfts('
            'identifier, name, last_price, last_price_asset, manual_price'
            ') VALUES(?, ?, ?, ?, ?)',
            (
                from_asset.identifier,
                from_asset.identifier,  # can't have the name here. Rethink?
                str(price),
                to_asset.identifier,
                1,
            ),
        )

        return True

    def delete_price_for_nft(self, asset: Asset) -> bool:
        cursor = self.db.conn.cursor()
        try:
            cursor.execute(
                'UPDATE nfts SET last_price=?, last_price_asset=? WHERE identifier=?',
                (None, None, asset.identifier),
            )
        except sqlcipher.DatabaseError as e:  # pylint: disable=no-member
            raise InputError(f'Failed to delete price for {asset.identifier} due to {str(e)}') from e  # noqa: E501
        if cursor.rowcount != 1:
            raise InputError(f'Failed to delete price for unknown asset {asset.identifier}')

        return True

    # -- Methods following the EthereumModule interface -- #
    def on_startup(self) -> None:
        pass

    def on_account_addition(self, address: ChecksumEthAddress) -> Optional[List['AssetBalance']]:
        pass

    def on_account_removal(self, address: ChecksumEthAddress) -> None:
        pass

    def deactivate(self) -> None:
        pass