🚀 Сборка и запуск контейнера

Клонируй репозиторий и перейди в папку проекта:

git clone https://github.com/stekirill/Buch-Bot.git
cd Buch-Bot


Положи файл google_credentials.json и файл .env, которые я тебе скину, в папку telegram_bot/.


Собери Docker-образ и запусти контейнер:

docker build -t buch-bot .

docker run -d --name buch-bot buch-bot


Бот автоматически запустится внутри контейнера.
