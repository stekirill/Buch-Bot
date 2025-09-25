from telegram_bot.database.repository import ClientRepository
from telegram_bot.database.models import Client

class ClientService:
    def __init__(self, client_repo: ClientRepository):
        self.client_repo = client_repo

    async def get_or_create_client(
        self,
        user_id: int,
        username: str | None,
        first_name: str,
    ) -> Client:
        client = await self.client_repo.get_one_or_none(user_id=user_id)
        if not client:
            client_data = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
            }
            client = await self.client_repo.add_one(client_data)
        return client
