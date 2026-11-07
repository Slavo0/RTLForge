# Архитектура Waveform Reader

Цель — добавлять поведение через небольшие классы и композицию объектов,
сохраняя точку запуска стабильной. Данные захвата отделены от настроек просмотра.
Утилитарные операции остаются функциями там, где им не нужны состояние и полиморфизм.

## Компоненты

| Модуль | Ответственность и основные классы |
|---|---|
| `ila_viewer/__main__.py` | Тонкая точка запуска пакета |
| `ila_viewer/application.py` | `WaveformApplication`: создание приложения и внедрение фабрики окна; `install_translations` (Qt `qtbase_ru`) |
| `ila_viewer/window.py` | `MainWindow`: сборка интерфейса и меню (Файл, Вид, Навигация, Анализ, Настройки, Справка), связывание событий, жизненный цикл захвата. Редакторы копирования и подсветки получают сигналы из дерева представления (`condition_nodes`), поэтому разрезанные биты VCD видны только внутри своей шины |
| `ila_viewer/sources.py` | `CaptureSource`, выбор CSV/VCD/WLF и адаптер штатного `wlf2vcd`; `SourceNavigator`: переход к HDL |
| `ila_viewer/data.py` | `Capture`, `SignalData`, потоковый CSV-парсер и дисковые индексы |
| `ila_viewer/vcd.py` | `VcdCaptureReader`: потоковый разбор VCD; `SplitBusAssembler`: объединение скалярных битов с непрерывным диапазоном индексов (в любом порядке объявления, показываются от старшего бита) без потери их исходных индексов |
| `ila_viewer/bits.py` | `BitCodec`, `BitExpansionService`: извлечение битов и их точных переходов |
| `ila_viewer/tree.py` | `SignalTree`, `WaveNode` и подклассы: дерево, порядок, группы, разделители; `DisplaySignal`: адаптер представления |
| `ila_viewer/formats.py` | `ValueFormat`, `FormatRegistry`: стратегии отображения и разбора поисковых значений |
| `ila_viewer/themes.py` | `Theme`, `ThemeManager`, `SignalPalette`: цвета, стили интерфейса, выбор контрастной палитры |
| `ila_viewer/icons.py` | `RowIconPainter`: векторные значки строк в форме того, что строка рисует на диаграмме |
| `ila_viewer/csv_chunks.py` | Векторный разбор блоков CSV, `ColumnEncoder`, `ValueTable`, функции процессов |
| `ila_viewer/cpu.py` | `CpuTopology` (ядра/потоки), `WorkerPool` — общий пул процессов |
| `ila_viewer/condition_vectors.py` | `Condition.evaluate` на массивах точек |
| `ila_viewer/actions.py` | `ContextActionProvider`, `ContextMenuController`: независимые поставщики команд меню |
| `ila_viewer/view.py` | `WaveformView`: координаты, масштаб, выделение, мышь/клавиатура, прокрутка |
| `ila_viewer/rendering.py` | `WaveformRenderer`: рисование видимого участка; не меняет данные и структуру дерева |
| `ila_viewer/workers.py` | `BackgroundTask`: выполнение отменяемых операций вне GUI-потока |
| `ila_viewer/preferences.py` | `ViewerPreferences`, `PreferencesStore`: только общие настройки, проверка типов и атомарная запись JSON |
| `ila_viewer/sampling.py` | `SamplingGrid`: `PeriodicSampling` (маркеры 1/2) и `EdgeSampling` (фронты тактового сигнала, в т.ч. неактивный такт SPI); точки `(start, end, at, previous_at)` для копирования и подсветок |
| `ila_viewer/timebase.py` | `TimeAxis` и форматирование времени в стиле Vivado: группировка разрядов, единица по шагу шкалы, без экспоненты; разбор «100 MHz» / «10 ns» |
| `ila_viewer/settings_dialog.py` | `AppearanceDialog`: вкладки «Общие», «Сигналы», «Подсветка», «Источники» с поштучным сбросом к значениям по умолчанию |
| `ila_viewer/selection.py` | `RowSelection`: диапазонное выделение по стабильным идентификаторам узлов и отдельный якорь |
| `ila_viewer/copying.py` | `CopyValuesService`, `CopyRequest`, `CopyChannel`: сборка выбранных битов, снимок настроек, отменяемый экспорт; `SystemClipboard`: запись в Qt clipboard |
| `ila_viewer/conditions.py` | Безопасный разбор условий Copy Values и разрешение коротких/полных имён сигналов |
| `ila_viewer/copy_dialog.py` | `CopyConditionDialog`: редактор условия с выбором сигнала и сохранением позиции курсора |
| `ila_viewer/period_dialog.py` | `CopyPeriodBanner`: немодальная панель такта выборки — сигнал такта или маркеры 1/2 |
| `ila_viewer/widgets.py` | `SegmentedChoice` (сегментный выбор с API как у `QComboBox`), `keycap`, `muted`, `plain_container` |
| `ila_viewer/assets/` | SVG-галочки для флажков (тёмная и светлая), подставляются в QSS темы |
| `ila_viewer/refresh.py` | Снимок оформления сигналов и правил подсветки для сверки после повторной загрузки CSV |
| `ila_viewer/highlights.py` | `HighlightService`, `HighlightIntervals`, `HighlightModel`: фоновый поиск, дисковые интервалы и состояние групп текущего захвата |
| `ila_viewer/bookmarks.py` | `Bookmark`, `BookmarkModel`: именованные маркеры текущего захвата, порядок, переход, ограничение длиной захвата |
| `ila_viewer/bookmarks_ui.py` | `BookmarkController`: команды меню «Навигация»; `BookmarkManagerDialog`: немодальный список маркеров |
| `ila_viewer/measurements.py` | `EdgeMeasurementService`, `EdgeMeasurement`, `MeasurementFormatter`: фронты однобитного сигнала между A и B, период и частота |
| `ila_viewer/spi.py` | Декодер SPI / QSPI без Qt: `LineRef`, `SpiPins`, `SpiSettings`, `CommandSpec`, `FixedFormat`; `SpiDecoder` (кадры CS → поля), `SpiDecodeResult` (дисковые кадры и поля), `DecodedTrack` (строка диаграммы), `FieldOverlay`/`ClockEdgeMarks`/`overlays` (подписи на проводах), `SpiExportService` (текст и `.bin`) |
| `ila_viewer/annotations.py` | `SignalAnnotations`: реестр аннотаций поверх строк сигналов по имени источника; владельцы (декодеры) задают и снимают свои аннотации |
| `ila_viewer/spi_ui.py` | `SpiController`: команды меню, жизненный цикл результата; `SpiDecoderDialog`, `SpiPanel` + `SpiTransactionModel`, `SpiContextActions` |
| `ila_viewer/highlights_ui.py` | `HighlightConditionDialog`, `HighlightPanel`: редактор условия и список групп над полосой прокрутки |
| `tests/` | Поведенческие тесты и искусственный CSV |
| `scripts/benchmark_waveform.py` | Воспроизводимая проверка большого искусственного захвата |

Новый код приложения размещается в пакете `ila_viewer/`. В корне нет Python-модулей;
`start.bat` запускает пакет через `python -m ila_viewer`.

## Принципы

- **Разделение ответственности:** формат значения не знает о Qt, модель дерева
  не рисует диаграмму, renderer не переносит строки, CSV-источник не выбирает тему.
- **Расширение без изменения запуска:** новые форматы регистрируются, команды
  добавляются поставщиками, тема передаётся менеджеру, источник внедряется в окно.
- **Композиция:** окно получает `source`, `themes`, `source_navigator`, `menus`.
  Подмена источника не требует наследовать большое окно.
- **Небольшие интерфейсы:** источник выдаёт захват, формат преобразует значение,
  поставщик меню добавляет команды, навигатор открывает исходник.
- **Неизменность захвата:** `DisplayOptions` и `DisplaySignal` хранят цвет, формат
  и Reverse Bit Order. Массивы CSV открыты только для чтения.

`AutoCaptureSource` выбирает источник по расширению. WLF читается через
`QuestaWlfConverter`, который запускает `wlf2vcd` без оболочки, поддерживает
отмену и передаёт временный VCD в `VcdCaptureReader`. При коде 7 с ошибкой
открытия WLF он вызывает `wlfrecover` только на временной копии и повторяет
конвертацию. `WlfExportService` атомарно сохраняет постоянный VCD для просмотра
без Questa. Временный VCD при обычном открытии удаляется
после построения дисковых индексов; `Capture.path` остаётся исходным WLF для
повторной загрузки. `Capture.times` содержит int64-отметки каждого события,
а `WaveformView` переводит индексы в координаты с учётом временных интервалов.
Выборка по периоду в редакторах пока использует индексы событий.
`SplitBusAssembler` создаёт дополнительный дисковый индекс для собранной шины.
Её исходные однобитные сигналы остаются в `Capture.signals`, но в `SignalTree`
показаны дочерними строками этой шины после раскрытия.

## Как добавить функцию

### Новый Radix

Реализовать `ValueFormat.format(bits, width, settings)` и `parse(text, width, settings)`
и зарегистрировать объект через `FORMATS.register(...)` до создания окна.
Контекстное меню и список Radix заполняются из реестра автоматически.
`parse` возвращает беззнаковый битовый шаблон, адаптер переводит его в представление
исходного CSV. `format` получает значение, уже ограниченное шириной шины.

### Новая команда контекстного меню

Создать класс `ContextActionProvider`, реализовать `contribute(menu, view)`.
Его экземпляр добавить в `ContextMenuController.providers` либо передать список
поставщиков конструктору. Объект контроллера внедрить как `MainWindow(menus=...)`.
Существующие поставщики не требуется редактировать.

### Новая тема или другой дизайн

Создать объект `Theme` со всеми цветовыми ролями и передать через
`ThemeManager([theme1, theme2, ...])` в `MainWindow(themes=...)`.
Меню тем создаётся автоматически. `Theme.stylesheet()` отвечает за оформление
контролов; renderer использует роли темы вместо фиксированных цветов.
Для более глубокого изменения диаграммы меняется `WaveformRenderer`,
для формы значков — `RowIconPainter`, без изменения CSV-парсера и запуска.

### UI conventions

The target displays are Full HD and 4K (Windows scaling up to 200%); the app runs
on Qt's Fusion style, so screenshots for review must use Fusion too.

- The waveform carries the color: signal colors, markers A/B, X/Z and decoded
  fields keep their meaning. The chrome around it uses the theme's neutral roles
  plus one accent, `Theme.accent` (the theme's bus color): keyboard focus, checked
  boxes and segments, progress, and buttons marked `primary`.
- Only buttons with the dynamic property `primary=True` get the accent fill. The
  `:default` state is not filled, because in dialogs it follows keyboard focus.
- Toolbar buttons are flat (background on hover only) and grouped by separators:
  file, zoom, navigation.
- Hairlines use `grid`; the A…B band is `Theme.a_b_band()` (marker A at low alpha).
- Labels and layout-only containers are transparent (`QWidget#plain`), so framed
  panels such as the clock banner show one surface.
- Two to four short, mutually exclusive options are a `SegmentedChoice`, not a combo
  box. Gestures are keycaps. Settings that only matter for one mode live on that
  mode's page or behind a toggle (IO2/IO3, the SPI command table), instead of
  explaining them in text.
- Dynamic properties used by the stylesheet: `primary`, `segment`/`first`/`last`,
  `keycap`, `role` (`muted`, `readout`, `error`).

### Go to Source Code

Реализовать `SourceNavigator.available(signal)` и `go_to(signal)` и передать объект
в `MainWindow(source_navigator=...)`. Сейчас подключён
`UnconfiguredSourceNavigator`, поэтому пункт виден, но недоступен.
В будущем здесь можно хранить таблицу `signal → HDL file, line` и открывать редактор.

### Будущий симулятор

Реализовать `CaptureSource.load(location, progress, cancel)` и вернуть `Capture`
с тем же контрактом сигналов: `starts` — начала интервалов, `codes` — индексы
значений, `values` — таблица значений. Затем передать источник в
`MainWindow(source=...)`. Весь просмотрщик работает с моделью захвата.

Это точка расширения для **готового результата симуляции**, а не уже готовый
движок симуляции или потоковое обновление в реальном времени. Для пошагового
редактирования и live simulation понадобится отдельный контроллер исполнения,
механизм публикации новых снимков данных и отдельные операции изменения модели.

## Дерево и владение ресурсами

`SignalTree` использует составные узлы: сигналы/шины/биты, группы, разделители,
параметры. Список видимых строк вычисляется из дерева. Идентификатор узла
и объект сохраняются при переносе; исходный порядок `Capture.signals` не меняется.
Нельзя создать цикл групп или вытащить физический бит из его шины.
При удалении группы удаляется её поддерево из модели просмотра, вместе с сигналами.
Содержимое `Capture` остаётся нетронутым. Это не операция разгруппировки.

Раскрытие шины вызывает `BitExpansionService` в фоне. Сервис блоками извлекает
биты, удаляет повторяющиеся интервалы и записывает индексы на диск. Результат
присоединяется к `Capture.derived` только после успешного завершения операции.
При отмене частичные индексы удаляются. Закрытие окна сначала запрашивает отмену
и ждёт завершения потока через событие, затем освобождает memmap и временную папку.

Reverse Bit Order меняет значения и диапазон в заголовке. Хранимый порядок дочерних
строк не меняется; при включённой настройке 
everse_bit_rows SignalTree.rows()
выдаёт раскрытые биты в обратном порядке, а перетаскивание битов учитывает это. Поиск инвертирует преобразование и обращается к исходному индексу.
При изменении ширины или порядка битов нельзя ограничиваться разворотом строкового
HEX-представления: преобразование выполняется на уровне битового шаблона.

## Общие настройки и состояние захвата

`PreferencesStore` внедряется в `MainWindow(preferences_store=...)`. Стандартное
хранилище использует `.viewer-settings.json` в папке проекта; `PreferencesStore()`
без пути работает в памяти. Тесты обязаны использовать память или собственный
временный путь. JSON ограничен полями `ViewerPreferences`: хранит последнюю папку
диалога открытия, цвета по темам и палитру подсветки, но не сериализует дерево,
содержимое CSV, группы подсветки, период ILA clock, индивидуальные цвета/Radix
или курсоры. Запись производится
через временный файл и замену, ширины колонок сохраняются с задержкой 300 мс,
закрытие окна выполняет окончательное сохранение. Ошибка чтения даёт значения по
умолчанию; ошибка записи показывается в строке состояния.

`WaveformView.display_radix` разрешает локальный Radix и общий Radix:
локальный Default → общий; общий Default → исходный формат CSV в `FormatRegistry`.
Обновление общего значения не записывает ничего в `DisplayOptions` отдельных
сигналов. Renderer, Value, подсказки и поиск используют один разрешённый формат.

Ширины Name и Value независимы и измеряются в логических пикселях Qt.
`plot_left` — их сумма; обычное изменение размеров окна не меняет эти ширины.
Автоподбор Name вызывается двойным щелчком; Value также увеличивается примерно
в полтора раза, когда видимое значение не помещается. Подбор использует метрики шрифта,
отступы дерева и строки текущего представления. `columnsChanged` связывает
изменение ширины с фильтром над Name и сохранением настроек.

`RowSelection` отделяет множество выделенных строк от активной строки и якоря.
Shift в колонках расширяет выделение, Shift на waveform управляет B.
Обычный щелчок по waveform выбирает строку и перемещает A, Ctrl+щелчок
переключает строку в выделении. Фильтр дерева учитывает совпадения в потомках,
но не изменяет `expanded`; свёрнутые дочерние строки остаются скрытыми.
Контекстные команды Radix/Color применяются к выделению. Правая кнопка по выделенной
строке сохраняет набор. Перенос `move_many` сначала проверяет весь набор, исключает
дочерние узлы уже выбранных родителей и затем атомарно перемещает строки с сохранением
порядка. Переход к краям при отсутствии
фронтов реализован в навигации `WaveformView`, не добавляет фиктивных изменений
в исходный `SignalData` и не меняет результаты поиска.

## Именованные маркеры и измерения

`BookmarkModel` — состояние представления, внедряемое в `WaveformView(bookmarks=...)`
(по умолчанию создаётся собственное). Оно не входит в `ViewerPreferences` и не меняет
`SignalData`. `MainWindow.loaded` очищает модель при открытии другого файла и вызывает
`clamp(count)` при обновлении, так что маркеры за концом нового захвата переносятся на
последний отсчёт. `WaveformRenderer.draw_bookmarks` рисует только маркеры видимого
окна. Команды меню и окно списка собраны в `BookmarkController`; окно лишь вызывает
`install(menu, window)`. Контекстные команды добавляет `BookmarkActions`, используя
`WaveformView.context_sample` — ближайший отсчёт правого щелчка.

`EdgeMeasurementService.measure(signal, a, b, cancel)` counts real 0→1 and 1→0
transitions in `(min(a,b), max(a,b)]`. Clean binary signals use O(log n) index
searches. Unknown-state signals scan at most 262,144 transitions per batch with
cancellation between batches. `measurements_ui.EdgeMeasurementController` coalesces
requests for 60 ms, runs scans in a dedicated `BackgroundTask`, and accepts results
only for the current signal/range. Capture replacement and close disable requests,
cancel/join the worker, and only then release memory maps. Loading resumes the
controller after installing the new capture. `MeasurementFormatter` uses exact
simulator tick differences or the general CSV sample period.

`EdgeSampling(times=capture.times)` measures simulator clocks in time: the typical
period is the median timestamp difference of real edges (`typical_ticks`) and the
gated-clock cap is one such period found with `searchsorted` on the timestamps.
CSV rows keep the row-count cap. `period_dialog.edge_statistics` computes the banner
readout once per (signal, edge); indices above `SYNC_RUNS` transitions are counted in
a cancellable thread. `HighlightIntervals.visible(..., edges)` bins dense intervals by
the renderer's pixel boundaries, so a VCD bin never spans an idle stretch.

Writes that replace a user file (`.bin`, settings) go through `files.atomic_write`: a
unique `mkstemp` sibling, `os.replace` on success, the old file kept on error.
`SpiExportService.write_binary` rejects UNKNOWN bytes. `WlfExportService` refuses a
destination that is the source file. Highlight groups store `radixes` (dependency →
literal radix) at creation; refresh recompiles with those, not the current view radix.

## Zoom и копирование

Ввод различает `zoom_pending`/`zoom` и перетаскивание маркера. Обычный щелчок
сразу ставит A и убирает B; zoom начинается только после порога перемещения мыши
при старте вне линии маркера. Renderer рисует временную полосу `zoom_band`.
После отпускания применяется диапазон не менее двух отсчётов; координаты
нормализуются для обеих сторон жеста. Маркеры и Shift+ЛКМ сохраняют свой режим.

`CopyValuesService.snapshot` фиксирует выбранные строки, диапазон, отдельный Radix
и набор битов каждого канала, разделитель, период выборки и режим уникальности.
Без B диапазон охватывает весь захват. Условие
разбирается `ConditionCompiler` в ограниченное логическое выражение без исполнения
пользовательского Python-кода; для каждого экспортируемого отсчёта его проверка
использует исходные индексы сигнала. Период и фазу задают два временных маркера
на waveform; в этом режиме A/B скрыты, но сохранены. Фаза экстраполируется назад
и вперёд как `first + k * period`, в том числе при начале CSV внутри периода.
Повторная настройка доступна из редактора условия; включается левая граница
между A/B и исключается правая независимо от порядка маркеров. Выбранные биты группируются
по родительской шине; их позиции берутся из исходного диапазона `BitCodec.labels`,
а не из визуального порядка строк. `CopyChannel` пакует биты и определяет ширину
формата. При выборе самой шины её дочерние биты не создают повторный канал.

`build` читает индексы блоками, ограничивает кэш преобразованных значений и размер
результата, регулярно проверяет отмену. В режиме уникальности значения
фильтруются после проекции на выбранные биты и при желании сортируются.
`MainWindow` записывает готовый результат в
clipboard только из GUI-потока. Реализация буфера внедряется параметром `clipboard`;
тесты используют объект в памяти и не трогают пользовательский буфер обмена.

`HighlightService` использует тот же `ConditionCompiler` и калибровку периода,
но создаёт интервалы вместо текста. Совпадающие соседние периоды сливаются,
пары границ пишутся во временный файл рядом с индексами захвата и читаются через
memmap. `WaveformRenderer` выбирает только интервалы видимого времени. Отмена,
удаление группы, повторная загрузка CSV и закрытие окна освобождают эти файлы.
`HighlightModel` хранит только состояние текущего захвата; в `PreferencesStore`
остаются пресеты цвета/непрозрачности и поведение общего выключателя; непрозрачность
созданной подсветки хранится в каждой группе захвата.
`ConditionCompiler` разбирает числа через текущий формат конкретного сигнала,
а префиксы `h`/`b`/`d` задают явное основание. `prev(signal)` хранится в узле
выражения как обращение к предыдущему калиброванному отсчёту. Сравнения
сохраняют порядок операндов, включая запись «литерал <= сигнал».
`AdvancedPattern` содержит 2–16 обычных условий-этапов; отдельный
`AdvancedHighlightService` ведёт активные пути совпадений по соседним отсчётам
сетки периода, допускает наложение последовательностей и пишет слитые
интервалы на диск. Режим группы хранится в `HighlightGroup` и
`CaptureRefreshState`, чтобы обновление CSV пересобирало её тем же движком.
Панель групп видна по умолчанию и располагает группы горизонтально. Опция общих
настроек скрывает только пустую панель. Отдельные цвета шин и
однобитных сигналов по темам также являются общими настройками.

При «Обновить CSV» `CaptureRefreshState` снимает оформление по полным именам
сигналов и определения подсветок, после загрузки применяет оформление к
совпавшим сигналам. Условия компилируются заново для нового захвата, а интервалы
подсветок строятся в фоновом задании; правила с исчезнувшими сигналами пропускаются.
Открытие другого CSV не переносит это состояние. Редакторы копирования и подсветки
немодальны: данные для фоновой операции фиксируются только при подтверждении.

## Загрузка и обработка на нескольких ядрах

NumPy kernels can release the GIL; parsing and dictionaries also perform Python work.
Compare backends by workload before changing the process policy; see [performance evidence](PERFORMANCE.md).

1. **Векторизация.** `csv_chunks.py` (без Qt) делит блок строк на ячейки по байтам
   (`split_block`), сравнивает соседние ячейки столбца массивом и разбирает только
   изменения. `ColumnEncoder` переводит HEX/BIN/OCT/DEC-ячейки в int64 массивами
   (`parse_digits`), X/Z и прочие идут через `parse_value` по одному, с кэшем текста.
   `ValueTable` выдаёт коды значений в порядке первого появления, как прежний
   построчный загрузчик; тест сравнивает оба (`tests/reference.py`).
   `Condition.evaluate(at, previous)` (`condition_vectors.py`) считает условие для
   массива точек: сравнение с литералом вычисляется один раз на каждое различное
   значение сигнала и раскладывается по точкам; X/Z и значения шире int64 идут через
   скалярные правила `Condition.matches`, поэтому семантика одна. `SamplingGrid.point_chunks`
   отдаёт точки сетки массивами; `IntervalWriter` сливает интервалы подсветки.
2. **Процессы.** `cpu.WorkerPool` — один пул (spawn) на окно, создаётся лениво,
   закрывается при выходе и передаётся источникам через конструктор
   (`AutoCaptureSource(pool=...)`). Число процессов — общая настройка `worker_count`
   (0 — авто: `CpuTopology.recommended_workers`, физические ядра минус одно).
   Пул ограничен 61 процессом на Windows (`max_pool_workers`); `set_workers` во время
   работы откладывает пересоздание пула до простоя и не отменяет текущие задачи.
   `load_csv` делит файл на диапазоны по границам строк (`line_aligned_ranges`),
   не меньше `MIN_PARALLEL_CHUNK` на процесс. Фаза 1 (`parse_range`): каждый процесс
   пишет свои локальные серии в файлы папки захвата и возвращает только таблицы
   значений. Основной процесс строит глобальные коды и смещения (`_plan_column`).
   Фаза 2 (`place_range`): процессы сами пишут свои серии в заранее выделенные
   итоговые файлы. Номера строк в ошибках глобальные; кавычки в файле — переход на
   один процесс; отмена — файл-флаг `cancel.flag`, который проверяется на каждом блоке.

Модули, которые импортируют процессы (`csv_chunks`, `data`, `cpu`), не импортируют Qt.
SPI-декодер (≈12 тыс. кадров/с) и VCD пока работают в одном процессе:
кадры SPI независимы, их можно разнести по процессам так же, если понадобится.
Для замеров: `python -m scripts.benchmark_waveform` и `python -m scripts.benchmark_parallel`
(свип 1…32 процессов, отчёт в `tests/artifacts/benchmark_parallel.json`).

## Декодер SPI / QSPI

`SpiDecoder.decode(capture, config, folder, progress, cancel)` — фоновая операция без Qt.
Кадры — интервалы активного CS из индекса серий CS; без CS весь захват один кадр.
Фронты SCK читаются блоками по индексу серий (`_EdgeStream`), значения IO0…IO3 —
бинарным поиском в индексах линий на позиции фронта или перед ним. Линия задаётся
`LineRef(signal, offset)`, поэтому подходит и однобитный сигнал, и бит шины.
Формат кадра описывает `_Plan`: в режиме `commands` он берётся из `CommandSpec`
по коду команды (с явными настройками протокола, размера адреса, dummy и DTR),
в `fixed` — из `FixedFormat`, в `raw` кадр сразу делится на байты. Фазы SDR берут
только фронты выборки, DTR — все фронты, начиная со следующего фронта выборки.
Поля пишутся в два дисковых файла структурированных записей (`FIELD_DTYPE`):
полоса 0 — команда, адрес, dummy и данные, полоса 1 — MISO полного дуплекса;
кадры — в `FRAME_DTYPE` со ссылками на свои поля. Поле кончается через полшага
передачи после своего последнего фронта (там, где кончается последний бит), но не
позже начала следующего поля, поэтому ни одна полоса не требует сортировки, а между
байтами остаются паузы. `DecodedTrack.transition` шагает от начала к началу поля и
останавливается на конце только у последнего поля кадра. Отмена
удаляет частичные файлы, `SpiDecodeResult.close` идемпотентно закрывает memmap и
удаляет файлы.

`DecodedNode` (вид `decoded`) — строка дерева с `DecodedTrack` вместо сигнала.
`WaveformRenderer.draw_track` рисует только поля видимого окна, а при большой
плотности — занятые пиксели (`DecodedTrack.busy`), так что работа ограничена
шириной окна. `WaveformView.current_navigable` возвращает сигнал или трек, поэтому
←/→ и Shift+←/→ работают по границам полей без особых случаев. Данные захвата
не меняются.

Подписи на проводах — слой представления. `spi.overlays(result, capture, tracks)`
возвращает пары (имя однобитного сигнала, аннотация): `FieldOverlay` — поля полосы
нужных видов только с одной линией (CMD/ADDR/WR/MOSI на IO0, RD/MISO на IO1),
`ClockEdgeMarks` — фронты выборки SCK внутри кадров CS. `SpiController` кладёт их в
`WaveformView.annotations` (`SignalAnnotations`) и снимает при закрытии результата.
`WaveformRenderer` для строки сигнала с аннотациями сдвигает осциллограмму вниз
(`band`), рисует блоки `draw_boxes` (подпись `bubble_label`) или занятые пиксели и
стрелки, только если они не сливаются; объём работы ограничен видимым окном.

`SpiController` получает `runner` (окно: `start_work`, `run_when_idle`, `worker`,
`clipboard`), ставит `SpiContextActions` в `ContextMenuController.providers` и
устанавливает команды в меню «Анализ». Экспорт строит `SpiExportService` в фоне;
буфер обмена пишется в GUI-потоке после успешного завершения. Окно вызывает
`capture_changed` до закрытия старого захвата (файлы декодера лежат в его
временной папке); при обновлении конфигурация переносится по именам линий, и
декодирование ставится в очередь `run_when_idle` после восстановления подсветок.
Конфигурация декодера — состояние захвата. В общие настройки
(`ViewerPreferences.spi_decoder`) попадает только последний выбор окна
(`config_to_dict`): он предзаполняет окно (`settings_from_dict`, `pins_from_dict` —
линии только если все они есть в захвате) и никогда не запускает декодирование сам.
`guess_pins` при отсутствии однобитных IO берёт биты шины из `data_buses`
(`bus_lines`: бит 0 — IO0). Кадр, где линии команды всё время в 1
(`SpiSettings.reset_frames`, `FF` нет в таблице), записывается одним полем
`RESET` с флагом кадра `RESET_FRAME`: проверка читает кадр целиком только после
байта `FF` и останавливается на первом нуле.
Новый протокол добавляется так же: Qt-независимый декодер с дисковым результатом,
трек с `window`/`busy`/`label_at`/`transition` и свой контроллер.

### Поиск по SPI

`SpiQuery` (в `spi.py`) разбирает `cmd 9F`, `@1000-1FFF`, `data 01 02` и
последовательность байтов. Поля дорожки разворачиваются в поток байтов (адрес — его
байты, dummy пропускается) блоками с перекрытием на длину шаблона; совпадение не
переходит границу кадра. `DecodedTrack.search` повторяет контракт
`SignalData.search` (начало поля, направление, кольцевой поиск) и кэширует найденные
позиции для F3. Панель транзакций фильтрует кадры через `SpiQuery.frames`.

## Проверки

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_waveform tests.test_extensions tests.test_review tests.test_remediation
.\.venv\Scripts\python.exe -m scripts.benchmark_waveform
```

Проверяются значения искусственного CSV, поиск, частичные X/Z, индексы диапазонов,
отмена и удаление ресурсов, работа дерева, форматы, мышиное перетаскивание,
курсоры, контекстные команды и темы. Изменения производительности проверяются
на синтетическом миллионе отсчётов, в том числе с раскрытой 9-битной шиной
и с поиском последовательности Advanced Highlight.

## Import, coordinate and ownership guarantees

VCD headers and body share a bounded-read whitespace-token stream. Dump commands,
scalar/vector changes and comments can span or share physical lines. CSV blocks
end at complete records, including quoted multiline cells; a parallel quote
encounter restarts through the single-process path. `_gather` budgets padded cell
storage and index scratch space separately (16 MiB each); ordinary columns retain
vectorized batches and skewed-width columns use compact byte objects.

`Capture.time_at` preserves integral absolute ticks as Python integers and
fractional absolute ticks as Decimal. Rendering and hit testing subtract a local
integer timestamp origin before any float conversion (`time_offset_at`,
`samples_at_offsets`); integer inverse queries search int64 timestamps directly.
`SimulatorTimeAxis` applies the same rule to rulers and elapsed-time readouts.

Simple and advanced highlight builders select grid points in ordered half-open
`[min(A,B), max(A,B))` ranges and clip painted spans to those boundaries. Explicit
condition bit selections retain their unsigned intent even when they select the
entire physical bus. A quoted complete signal name without a following selection
retains its original signedness.

CSV merge copies runs in 262,144-run batches, checks the cancellation flag between
batches, and cancels queued futures before waiting for active workers to release
files. Value-table remapping checks cancellation between batches too. SPI CS
frame discovery yields spans from bounded transition blocks with a carried active
start, rather than materializing every frame. Completed task output that is not
transferred into the view is released by `workers.dispose_result`, including lists
of restored `HighlightGroup` owners, before capture storage cleanup.
