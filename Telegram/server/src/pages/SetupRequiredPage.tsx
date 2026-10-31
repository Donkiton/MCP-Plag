import type { FC } from "hono/jsx";
import { TgPanel } from "./TgPanel.js";

export const SetupRequiredPage: FC = () => (
  <TgPanel title="Подготовка Telegram">
    <h1>Нужна первоначальная настройка</h1>
    <p>Владелец сервера ещё не указал API ID и API Hash приложения Telegram.</p>
    <p>Откройте закрытую форму настройки, сохраните реквизиты и вернитесь к подключению плагина.</p>
    <a class="tg-button" href="/setup">
      Открыть настройку
    </a>
  </TgPanel>
);
