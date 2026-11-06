"""User-facing bot texts. The language follows the user's Telegram language:
Kazakh for kk, Russian for ru/uk/be/uz/ky, English for everyone else.
Output files (Excel, 1C) are Russian for Kazakh users: accounting and 1C in Kazakhstan use Russian terms.
"""

RU_LANGUAGES = ("ru", "uk", "be", "uz", "ky")

TEXTS = {
    "ru": {
        "WELCOME": """Привет! Я превращаю фото и PDF документов в таблицу Excel или файл для загрузки в 1С.

Что умею:
• чеки, счета, инвойсы, акты выполненных работ и накладные;
• несколько чеков на одном фото — разберу каждый отдельно;
• проверяю суммы в каждом документе, сомнительные места подсвечиваю жёлтым;
• длинный чек можно сфотографировать частями;
• сфотографируйте договор, справку или письмо — пришлю настоящий цифровой PDF с текстом, как из Word;
• сделаю из фото аккуратный скан в PDF, как сканер: обрежу фон, выпрямлю лист, уберу тени — бесплатно.

Как пользоваться:
1. Пришлите фото или PDF (можно сразу несколько).
2. Нажмите, что это за документы.
3. Выберите формат: Excel или для 1С. Или нажмите «Цифровой документ» / «Скан в PDF».

Для лучшего качества отправляйте фото как файл (скрепка → «Файл»), без сжатия.

Осталось у вас документов: {remaining}. Тарифы: /plans

🔒 Файлы передаются по защищённому соединению и удаляются сразу после ответа. Подробнее: /privacy""",
        "PRIVACY": """🔒 Как мы обращаемся с вашими документами

• Распознавание полностью автоматическое: ваши документы не просматривает ни один человек.
• Файлы передаются только по зашифрованному соединению.
• Ваши документы не используются для обучения.
• Мы не храним файлы: они находятся в памяти только во время обработки и удаляются сразу после ответа.
• Сохраняется только ваш Telegram ID и число обработанных документов — для учёта бесплатного лимита.""",
        "BALANCE": "Осталось документов: {remaining}.",
        "PLANS": """💳 Тарифы

{prices}

Как купить:
1. Напишите {contact} и назовите пакет.
2. Оплатите удобным способом.
3. Документы начислятся на ваш аккаунт. Ваш ID: {user_id}

Осталось у вас: {remaining}.
Бесплатно и без ограничений: «Скан в PDF».""",
        "PLANS_NO_PRICES": "Цены уточняйте у владельца сервиса.",
        "PLANS_NO_CONTACT": "владельцу сервиса",
        "FILE_RECEIVED": "Получено файлов: {count}. Можете прислать ещё или выбрать, что это за документы:",
        "CHOOSE_FORMAT": "Спишется документов: {docs} (1 фото или 1 страница PDF = 1 документ, подробнее — /plans). В каком формате прислать результат?",
        "PROCESSING": "Обрабатываю, документов: {docs}. Это займёт примерно {seconds} сек…",
        "DONE": "Готово: документов — {docs}. Требуют проверки: {review} (подсвечены жёлтым, причина — на листе «Проверка» или в колонке «Замечания»).",
        "DONE_FAILED": " Не удалось обработать: {failed} (они отмечены в файле).",
        "PRICING_RULE": "1 фото или 1 страница PDF = 1 документ. Несколько чеков на одном фото — по числу чеков. Длинный чек частями — по числу фото (в файле это будет один чек). Очень плотная страница (больше ~50 строк) может считаться за 2–3 документа.",
        "PDF_TOO_MANY_PAGES": "В этом PDF {pages} стр., а за раз я принимаю не больше {limit}. Разделите файл на части и пришлите по очереди.",
        "LONG_TOO_MANY_PARTS": "Длинный чек — не больше {limit} фото, а сейчас {count}. Нажмите «Очистить» и пришлите чек заново, не больше {limit} фото.",
        "CHARGED": " Списано документов: {charged}.",
        "DONE_NOT_FOUND": " Документ не найден: {count} (на фото или странице нет документа или он не читается).",
        "BTN_FB_GOOD": "👍 Всё верно",
        "BTN_FB_BAD": "👎 Есть ошибка",
        "FB_THANKS": "Спасибо за оценку!",
        "FB_ASK": "Напишите одним сообщением, что не так — например: «в чеке 2 неверная дата». Файлы мы не храним, поэтому опишите ошибку словами. Мы разберём и сделаем распознавание точнее.",
        "FB_RECEIVED": "Спасибо! Передали разработчику — это поможет сделать распознавание точнее.",
        "CLEARED": "Список файлов очищен.",
        "NOTHING_TO_PROCESS": "Сначала пришлите фото или PDF документов.",
        "BUSY": "Подождите, ещё обрабатываю предыдущие документы.",
        "UNSUPPORTED_FILE": "Этот файл не подходит: {reason}. Принимаю фото (JPG, PNG, WEBP) и PDF.",
        "HEIC_FILE": "Фото в формате HEIC не поддерживается. Отправьте его как обычное фото или в JPG.",
        "TOO_LARGE": "Файл больше 20 МБ, такой Telegram не даёт скачать боту. Уменьшите или разделите его.",
        "TOO_MUCH_DATA": "Слишком много данных за раз (больше 100 МБ). Обработайте эти файлы, а остальные пришлите следующей партией.",
        "SERVER_BUSY": "Сервис сейчас загружен. Попробуйте через пару минут.",
        "SCAN_DAILY_LIMIT": "Бесплатный скан — до {limit} страниц в день, сегодня осталось {left}. Попробуйте завтра или пришлите меньше страниц.",
        "TOO_MANY_FILES": "За один раз — не больше {limit} файлов. Нажмите, что это за документы, а остальные пришлите следующей партией.",
        "NO_QUOTA": "Не хватает документов: нужно {needed}, осталось {remaining}.",
        "BUY": "\n\nЧтобы получить больше документов, напишите {contact}.{prices}\nПодробнее: /plans",
        "DAILY_LIMIT": "Сервис сегодня перегружен, попробуйте завтра. Извините!",
        "ERROR": "Что-то пошло не так при обработке. Попробуйте ещё раз чуть позже.",
        "GRANTED": "Начислено {docs} документов пользователю {user_id}. Теперь у него: {remaining}.",
        "GRANT_USAGE": "Использование: /grant <user_id> <количество>",
        "GRANT_NOTICE": "Вам начислено {docs} документов. Теперь доступно: {remaining}. Спасибо!",
        "STATS": "Пользователей: {users} (новых сегодня: {new_today})\nДокументов всего: {docs_total}\nСегодня: {docs_today} из {daily_limit}\n\nСпрос:\nДошли до лимита: {exhausted}\nСмотрели тарифы: {plans_viewed}\nКупили (есть начисления): {paid_users}\n\nОценки результатов: 👍 {fb_good} · 👎 {fb_bad}",
        "YOUR_ID": "Ваш Telegram ID: {user_id}",
        "BTN_RECEIPTS": "🧾 Чеки",
        "BTN_INVOICES": "📄 Счета / инвойсы",
        "BTN_ACTS": "📑 Акты работ",
        "BTN_WAYBILLS": "🚚 Накладные",
        "BTN_LONG": "🧾 Один длинный чек частями",
        "BTN_CLEAR": "🗑 Очистить",
        "BTN_EXCEL": "📊 Excel",
        "BTN_1C": "🏢 Для 1С",
        "BTN_PDF": "📷 Скан в PDF (бесплатно)",
        "BTN_DIGITIZE": "📝 Цифровой документ (PDF с текстом)",
        "DIGITIZE_PROCESSING": "Набираю документ заново, страниц: {count}. Это займёт примерно {seconds} сек…",
        "DIGITIZE_DONE": "Готово: цифровой документ, {pages} стр. Текст можно выделять, копировать и искать. Подписи и печати отмечены пометками [подпись], [печать] — их не копируем.",
        "PDF_PROCESSING": "Собираю PDF из файлов: {count}…",
        "PDF_DONE": "Готово: PDF из {count} стр. Это бесплатно и не расходует ваш лимит.",
        "PHONE_ASK": "Подтверждение номера для пробного периода на сайте Skaner. Нажмите кнопку ниже: Telegram отправит ваш номер. Сам номер мы не храним, только его необратимый отпечаток.",
        "BTN_SHARE_PHONE": "📱 Поделиться номером",
        "PHONE_NOT_OWN": "Нужен ваш собственный номер: нажмите кнопку «Поделиться номером» ниже. Чужой или пересланный контакт не подходит.",
        "PHONE_OK": "Готово, номер подтверждён. Вернитесь на сайт и нажмите «Проверить».",
        "PHONE_USED": "Этот номер уже получал пробный период. Войдите в тот аккаунт или напишите нам.",
        "PHONE_OTHER": "К вашему аккаунту на сайте уже привязан другой номер.",
        "PHONE_EXPIRED": "Ссылка устарела. Получите новую на сайте и откройте её снова.",
        "PHONE_UNAVAILABLE": "Сервис сейчас недоступен. Попробуйте позже: получите новую ссылку на сайте.",
        "PHONE_OFF": "Подтверждение номера сейчас недоступно.",
    },
    "kk": {
        "WELCOME": """Сәлем! Мен құжаттардың фотосы мен PDF-ін Excel кестесіне немесе 1С-ке жүктеуге арналған файлға айналдырамын.

Не істей аламын:
• чектер, шоттар, инвойстар, орындалған жұмыстар актілері және жүкқұжаттар;
• бір фотодағы бірнеше чек — әрқайсысын бөлек өңдеймін;
• әр құжаттағы сомаларды тексеремін, күмәнді жерлерін сары түспен белгілеймін;
• ұзын чекті бөліп-бөліп суретке түсіруге болады;
• шартты, анықтаманы немесе хатты суретке түсіріңіз — Word-тағыдай мәтіні бар нағыз цифрлық PDF жіберемін;
• фотодан сканердегідей таза PDF жасаймын: фонды кесемін, парақты түзетемін, көлеңкені кетіремін — тегін.

Қалай пайдалану керек:
1. Фото немесе PDF жіберіңіз (бірден бірнешеуін жіберуге болады).
2. Бұл қандай құжаттар екенін таңдаңыз.
3. Форматты таңдаңыз: Excel немесе 1С үшін. Немесе «Цифрлық құжат» / «PDF-ке скан» батырмасын басыңыз.

Сапасы жақсы болуы үшін фотоны файл ретінде жіберіңіз (қыстырғыш → «Файл»), сығымдаусыз.

Сізде құжат қалды: {remaining}. Тарифтер: /plans

🔒 Файлдар қорғалған байланыс арқылы жіберіледі және жауаптан кейін бірден жойылады. Толығырақ: /privacy""",
        "PRIVACY": """🔒 Құжаттарыңызбен қалай жұмыс істейміз

• Тану толығымен автоматты: құжаттарыңызды ешбір адам қарамайды.
• Файлдар тек шифрланған байланыс арқылы жіберіледі.
• Құжаттарыңыз оқыту үшін пайдаланылмайды.
• Біз файлдарды сақтамаймыз: олар тек өңдеу кезінде жадта болады және жауаптан кейін бірден жойылады.
• Тек сіздің Telegram ID және өңделген құжаттар саны сақталады — тегін лимитті есептеу үшін.""",
        "BALANCE": "Қалған құжаттар: {remaining}.",
        "PLANS": """💳 Тарифтер

{prices}

Қалай сатып алуға болады:
1. {contact} жазыңыз және пакетті атаңыз.
2. Ыңғайлы тәсілмен төлеңіз.
3. Құжаттар аккаунтыңызға қосылады. Сіздің ID: {user_id}

Сізде қалды: {remaining}.
Тегін және шектеусіз: «PDF-ке скан».""",
        "PLANS_NO_PRICES": "Бағаларды сервис иесінен нақтылаңыз.",
        "PLANS_NO_CONTACT": "сервис иесіне",
        "FILE_RECEIVED": "Алынған файлдар: {count}. Тағы жіберуге немесе бұл қандай құжаттар екенін таңдауға болады:",
        "CHOOSE_FORMAT": "Есептен шығарылатын құжаттар: {docs} (1 фото немесе PDF-тің 1 беті = 1 құжат, толығырақ — /plans). Нәтижені қандай форматта жіберейін?",
        "PROCESSING": "Өңдеп жатырмын, құжаттар: {docs}. Шамамен {seconds} секунд кетеді…",
        "DONE": "Дайын: құжаттар — {docs}. Тексеруді қажет етеді: {review} (сары түспен белгіленген, себебі — «Проверка» парағында немесе «Замечания» бағанында).",
        "DONE_FAILED": " Өңдеу мүмкін болмады: {failed} (олар файлда белгіленген).",
        "PRICING_RULE": "1 фото немесе PDF-тің 1 беті = 1 құжат. Бір фотода бірнеше чек болса — чек санымен. Бөліп түсірілген ұзын чек — фото санымен (файлда бір чек болады). Өте тығыз бет (~50 жолдан көп) 2–3 құжат болып есептелуі мүмкін.",
        "PDF_TOO_MANY_PAGES": "Бұл PDF-те {pages} бет, ал мен бір рет {limit} беттен артық қабылдамаймын. Файлды бөліктерге бөліп, кезекпен жіберіңіз.",
        "LONG_TOO_MANY_PARTS": "Ұзын чек {limit} фотодан аспауы керек, қазір {count}. «Тазарту» басып, чекті қайта жіберіңіз, {limit} фотодан аспасын.",
        "CHARGED": " Есептен шығарылған құжаттар: {charged}.",
        "DONE_NOT_FOUND": " Құжат табылмады: {count} (фотода немесе бетте құжат жоқ немесе ол оқылмайды).",
        "BTN_FB_GOOD": "👍 Бәрі дұрыс",
        "BTN_FB_BAD": "👎 Қате бар",
        "FB_THANKS": "Бағалағаныңызға рақмет!",
        "FB_ASK": "Не дұрыс емес екенін бір хабарламамен жазыңыз — мысалы: «2-чекте күні қате». Файлдарды сақтамаймыз, сондықтан қатені сөзбен сипаттаңыз. Біз қарап, тануды дәлірек етеміз.",
        "FB_RECEIVED": "Рақмет! Әзірлеушіге жібердік — бұл тануды дәлірек етуге көмектеседі.",
        "CLEARED": "Файлдар тізімі тазартылды.",
        "NOTHING_TO_PROCESS": "Алдымен құжаттардың фотосын немесе PDF-ін жіберіңіз.",
        "BUSY": "Күте тұрыңыз, алдыңғы құжаттарды әлі өңдеп жатырмын.",
        "UNSUPPORTED_FILE": "Бұл файл жарамайды: {reason}. Фото (JPG, PNG, WEBP) және PDF қабылдаймын.",
        "HEIC_FILE": "HEIC форматындағы фото қолдау көрсетілмейді. Оны кәдімгі фото ретінде немесе JPG форматында жіберіңіз.",
        "TOO_LARGE": "Файл 20 МБ-тан үлкен, Telegram мұндай файлды ботқа жүктеуге рұқсат бермейді. Оны кішірейтіңіз немесе бөліңіз.",
        "TOO_MUCH_DATA": "Бір реттік деректер тым көп (100 МБ-тан артық). Осы файлдарды өңдеңіз, қалғанын келесі жолы жіберіңіз.",
        "SERVER_BUSY": "Сервис қазір жүктелген. Бірнеше минуттан кейін қайталап көріңіз.",
        "SCAN_DAILY_LIMIT": "Тегін скан — күніне {limit} бетке дейін, бүгін {left} қалды. Ертең қайталаңыз немесе азырақ бет жіберіңіз.",
        "TOO_MANY_FILES": "Бір реттен {limit} файлдан аспауы керек. Құжат түрін таңдаңыз, қалғанын келесі жолы жіберіңіз.",
        "NO_QUOTA": "Құжаттар жетпейді: {needed} керек, {remaining} қалды.",
        "BUY": "\n\nКөбірек құжат алу үшін {contact} жазыңыз.{prices}\nТолығырақ: /plans",
        "DAILY_LIMIT": "Бүгін сервис бос емес, ертең қайталап көріңіз. Кешіріңіз!",
        "ERROR": "Өңдеу кезінде қате шықты. Сәл кейінірек қайталап көріңіз.",
        "GRANTED": "{user_id} пайдаланушыға {docs} құжат қосылды. Енді оның: {remaining}.",
        "GRANT_USAGE": "Қолдану: /grant <user_id> <саны>",
        "GRANT_NOTICE": "Сізге {docs} құжат қосылды. Қазір қолжетімді: {remaining}. Рақмет!",
        "STATS": "Пайдаланушылар: {users} (бүгін жаңа: {new_today})\nБарлық құжаттар: {docs_total}\nБүгін: {docs_today} / {daily_limit}\n\nСұраныс:\nЛимитке жеткендер: {exhausted}\nТарифтерді қарағандар: {plans_viewed}\nСатып алғандар: {paid_users}\n\nНәтиже бағалары: 👍 {fb_good} · 👎 {fb_bad}",
        "YOUR_ID": "Сіздің Telegram ID: {user_id}",
        "BTN_RECEIPTS": "🧾 Чектер",
        "BTN_INVOICES": "📄 Шоттар / инвойстар",
        "BTN_ACTS": "📑 Жұмыс актілері",
        "BTN_WAYBILLS": "🚚 Жүкқұжаттар",
        "BTN_LONG": "🧾 Бөліп түсірілген бір ұзын чек",
        "BTN_CLEAR": "🗑 Тазарту",
        "BTN_EXCEL": "📊 Excel",
        "BTN_1C": "🏢 1С үшін",
        "BTN_PDF": "📷 PDF-ке скан (тегін)",
        "PDF_PROCESSING": "Файлдардан PDF жасап жатырмын: {count}…",
        "PDF_DONE": "Дайын: {count} беттік PDF. Бұл тегін және лимитіңізді жұмсамайды.",
        "BTN_DIGITIZE": "📝 Цифрлық құжат (мәтіні бар PDF)",
        "DIGITIZE_PROCESSING": "Құжатты қайта теріп жатырмын, беттер: {count}. Шамамен {seconds} секунд кетеді…",
        "DIGITIZE_DONE": "Дайын: цифрлық құжат, {pages} бет. Мәтінді белгілеуге, көшіруге және іздеуге болады. Қолтаңба мен мөр [подпись], [печать] белгілерімен көрсетілген — оларды көшірмейміз.",
        "PHONE_ASK": "Skaner сайтындағы сынақ мерзімі үшін нөмірді растау. Төмендегі батырманы басыңыз: Telegram нөміріңізді жібереді. Нөмірдің өзін сақтамаймыз, тек оның қайтымсыз ізін сақтаймыз.",
        "BTN_SHARE_PHONE": "📱 Нөмірмен бөлісу",
        "PHONE_NOT_OWN": "Өз нөміріңіз керек: төмендегі «Нөмірмен бөлісу» батырмасын басыңыз. Басқа адамның немесе қайта жіберілген контакт жарамайды.",
        "PHONE_OK": "Дайын, нөмір расталды. Сайтқа оралып, «Тексеру» басыңыз.",
        "PHONE_USED": "Бұл нөмір сынақ мерзімін бұрын алған. Сол аккаунтқа кіріңіз немесе бізге жазыңыз.",
        "PHONE_OTHER": "Сайттағы аккаунтыңызға басқа нөмір байланған.",
        "PHONE_EXPIRED": "Сілтеме ескірген. Сайттан жаңасын алып, қайта ашыңыз.",
        "PHONE_UNAVAILABLE": "Сервис қазір қолжетімсіз. Кейінірек қайталаңыз: сайттан жаңа сілтеме алыңыз.",
        "PHONE_OFF": "Нөмірді растау қазір қолжетімсіз.",
    },
    "en": {
        "WELCOME": """Hi! I turn photos and PDFs of documents into an Excel spreadsheet.

What I can do:
• receipts, invoices and bills, acts of completed work and waybills;
• several receipts in one photo — each one comes out separately;
• every total is checked, doubtful values are highlighted in yellow;
• a long receipt can be photographed in parts;
• photograph a contract, letter or certificate — get back a real digital PDF with text, like from Word;
• turn photos into a clean scanned PDF, like a scanner app: background cropped, page straightened, shadows removed — free.

How to use:
1. Send photos or PDFs (several at once is fine).
2. Tap what kind of documents they are.
3. Choose the format, or tap "Digital document" / "Scan to PDF".

For best quality, send photos as files (paperclip → "File"), without compression.

Documents left: {remaining}. Plans: /plans

🔒 Files are sent over a secure connection and deleted right after the reply. Details: /privacy""",
        "PRIVACY": """🔒 How we handle your documents

• Recognition is fully automatic: no person ever looks at your documents.
• Files travel only over an encrypted connection.
• Your documents are not used for training.
• We don't store files: they stay in memory only during processing and are deleted right after the reply.
• We only keep your Telegram ID and the number of processed documents, to track the free allowance.""",
        "BALANCE": "Documents left: {remaining}.",
        "PLANS": """💳 Plans

{prices}

How to buy:
1. Message {contact} and name the plan.
2. Pay in a convenient way.
3. The documents are added to your account. Your ID: {user_id}

You have left: {remaining}.
Free and unlimited: "Scan to PDF".""",
        "PLANS_NO_PRICES": "Ask the service owner for prices.",
        "PLANS_NO_CONTACT": "the service owner",
        "FILE_RECEIVED": "Files received: {count}. Send more, or tap what kind of documents these are:",
        "CHOOSE_FORMAT": "Documents to be used: {docs} (1 photo or 1 PDF page = 1 document, details: /plans). Which format do you want?",
        "PROCESSING": "Processing {docs} document(s). This takes about {seconds} s…",
        "DONE": "Done: {docs} document(s). Need checking: {review} (highlighted in yellow, the reason is on the Review sheet).",
        "DONE_FAILED": " Could not process: {failed} (marked in the file).",
        "PRICING_RULE": "1 photo or 1 PDF page = 1 document. Several receipts in one photo count as several. A long receipt in parts counts by photos (it comes out as one receipt). A very dense page (over ~50 lines) may count as 2–3 documents.",
        "PDF_TOO_MANY_PAGES": "This PDF has {pages} pages; I accept up to {limit} at a time. Please split the file and send the parts one by one.",
        "LONG_TOO_MANY_PARTS": "A long receipt can have up to {limit} photos, now there are {count}. Press “Clear” and send the receipt again, up to {limit} photos.",
        "CHARGED": " Documents used: {charged}.",
        "DONE_NOT_FOUND": " No document found: {count} (the photo or page has no document or it is unreadable).",
        "BTN_FB_GOOD": "👍 All correct",
        "BTN_FB_BAD": "👎 Found a mistake",
        "FB_THANKS": "Thanks for the feedback!",
        "FB_ASK": "Please describe in one message what is wrong — for example: “wrong date on receipt 2”. We don't keep files, so please describe the mistake in words. We'll look into it and make recognition more accurate.",
        "FB_RECEIVED": "Thank you! Passed on to the developer — it helps make recognition more accurate.",
        "CLEARED": "File list cleared.",
        "NOTHING_TO_PROCESS": "Send photos or PDFs of your documents first.",
        "BUSY": "Please wait, I'm still processing your previous documents.",
        "UNSUPPORTED_FILE": "This file can't be used: {reason}. I accept photos (JPG, PNG, WEBP) and PDFs.",
        "HEIC_FILE": "HEIC photos are not supported. Send it as a regular photo or as JPG.",
        "TOO_LARGE": "The file is over 20 MB, Telegram doesn't let bots download it. Please shrink or split it.",
        "TOO_MUCH_DATA": "Too much data at once (over 100 MB). Process these files and send the rest in the next batch.",
        "SERVER_BUSY": "The service is busy right now. Please try again in a couple of minutes.",
        "SCAN_DAILY_LIMIT": "Free scanning is up to {limit} pages a day, {left} left today. Try again tomorrow or send fewer pages.",
        "TOO_MANY_FILES": "Up to {limit} files at a time. Tap the document type now and send the rest in the next batch.",
        "NO_QUOTA": "Not enough documents left: {needed} needed, {remaining} left.",
        "BUY": "\n\nTo get more documents, message {contact}.{prices}\nDetails: /plans",
        "DAILY_LIMIT": "The service is busy today, please try again tomorrow. Sorry!",
        "ERROR": "Something went wrong. Please try again a bit later.",
        "GRANTED": "Granted {docs} documents to user {user_id}. They now have: {remaining}.",
        "GRANT_USAGE": "Usage: /grant <user_id> <count>",
        "GRANT_NOTICE": "You received {docs} documents. Available now: {remaining}. Thank you!",
        "STATS": "Users: {users} (new today: {new_today})\nDocuments total: {docs_total}\nToday: {docs_today} of {daily_limit}\n\nDemand:\nReached the limit: {exhausted}\nViewed plans: {plans_viewed}\nBought (have credits): {paid_users}\n\nResult ratings: 👍 {fb_good} · 👎 {fb_bad}",
        "YOUR_ID": "Your Telegram ID: {user_id}",
        "BTN_RECEIPTS": "🧾 Receipts",
        "BTN_INVOICES": "📄 Invoices / bills",
        "BTN_ACTS": "📑 Acts of work",
        "BTN_WAYBILLS": "🚚 Waybills",
        "BTN_LONG": "🧾 One long receipt in parts",
        "BTN_CLEAR": "🗑 Clear",
        "BTN_EXCEL": "📊 Excel",
        "BTN_1C": "🏢 For 1C (Russian)",
        "BTN_PDF": "📷 Scan to PDF (free)",
        "BTN_DIGITIZE": "📝 Digital document (PDF with text)",
        "DIGITIZE_PROCESSING": "Re-typing the document, {count} page(s). This takes about {seconds} s…",
        "DIGITIZE_DONE": "Done: a {pages}-page digital document. The text can be selected, copied and searched. Signatures and stamps are marked as [signature], [stamp] — they are never copied.",
        "PDF_PROCESSING": "Building a PDF from {count} file(s)…",
        "PDF_DONE": "Done: a {count}-page PDF. It's free and doesn't use your allowance.",
        "PHONE_ASK": "Confirming your number for the trial on the Skaner website. Tap the button below: Telegram will send your number. We do not keep the number itself, only an irreversible fingerprint.",
        "BTN_SHARE_PHONE": "📱 Share my number",
        "PHONE_NOT_OWN": "We need your own number: tap the \"Share my number\" button below. Someone else's or a forwarded contact does not work.",
        "PHONE_OK": "Done, your number is confirmed. Go back to the website and tap \"Check\".",
        "PHONE_USED": "This number has already received a trial. Sign in to that account or contact us.",
        "PHONE_OTHER": "A different number is already linked to your account on the website.",
        "PHONE_EXPIRED": "The link has expired. Get a new one on the website and open it again.",
        "PHONE_UNAVAILABLE": "The service is unavailable right now. Please try later: get a new link on the website.",
        "PHONE_OFF": "Phone confirmation is not available right now.",
    },
}


def language_of(language_code: str | None) -> str:
    """Bot language for a Telegram user's interface language."""
    code = (language_code or "").lower()
    if code.startswith("kk"):
        return "kk"
    return "ru" if code.startswith(RU_LANGUAGES) else "en"


def files_language(lang: str) -> str:
    """Language of Excel/1C files for a bot language."""
    return "en" if lang == "en" else "ru"


def t(lang: str, key: str, **values) -> str:
    text = TEXTS.get(lang, TEXTS["en"])[key]
    return text.format(**values) if values else text
