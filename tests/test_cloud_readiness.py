import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app.cloud_worker import main, polling_lease, validate, build


class CloudReadinessTest(unittest.TestCase):
    def test_build_installs_only_bridge_without_polling(self):
        values = dict(student_os_bridge_enabled=True, outbox_database_url='postgresql://example',
                      student_os_api_url='https://core.example', student_os_bridge_secret='x'*48,
                      telegram_bot_token='synthetic')
        settings = SimpleNamespace(**values)
        with patch('app.cloud_worker.Application') as factory, patch('app.cloud_worker.install') as install:
            app = factory.builder.return_value.token.return_value.post_init.return_value.post_shutdown.return_value.build.return_value
            self.assertIs(build(settings), app)
            install.assert_called_once_with(app, settings)
            app.run_polling.assert_not_called()

    def test_polling_disabled_before_credentials_or_bot_creation(self):
        with patch.dict(os.environ, {}, clear=True), patch('app.cloud_worker.load_settings') as load:
            with self.assertRaisesRegex(RuntimeError, 'polling disabled'):
                main()
            load.assert_not_called()

    def test_enabled_worker_holds_database_lease_for_entire_polling_run(self):
        settings = SimpleNamespace(outbox_database_url='postgresql://example')
        application = SimpleNamespace(run_polling=lambda **kwargs: calls.append(kwargs))
        calls = []
        lease = patch('app.cloud_worker.polling_lease')
        with patch.dict(os.environ, {'CLOUD_POLLING_ENABLED':'true'}, clear=True), \
             patch('app.cloud_worker.load_settings', return_value=settings), \
             patch('app.cloud_worker.build', return_value=application), \
             patch('app.cloud_worker.require_no_webhook', new=unittest.mock.AsyncMock()), \
             patch('app.observability.initialize'), \
             lease as mocked_lease:
            main()
        mocked_lease.assert_called_once_with('postgresql://example')
        self.assertEqual(calls, [{'allowed_updates': __import__('telegram').Update.ALL_TYPES}])

    def test_webhook_mode_blocks_polling_before_settings(self):
        with patch.dict(os.environ, {'CLOUD_POLLING_ENABLED':'true',
                                    'TELEGRAM_DELIVERY_MODE':'webhook'}, clear=True), \
             patch('app.cloud_worker.load_settings') as load:
            with self.assertRaisesRegex(RuntimeError, 'webhook delivery mode'):
                main()
            load.assert_not_called()

    def test_registered_webhook_is_never_deleted_by_polling(self):
        from app.cloud_worker import require_no_webhook
        bot = unittest.mock.AsyncMock()
        bot.get_webhook_info.return_value = SimpleNamespace(url='https://example.invalid/webhook')
        with self.assertRaisesRegex(RuntimeError, 'Webhook exists'):
            __import__('asyncio').run(require_no_webhook(SimpleNamespace(bot=bot)))
        bot.delete_webhook.assert_not_called()

    def test_polling_lease_rejects_second_worker_and_releases_first(self):
        connection = unittest.mock.Mock()
        connection.execute.return_value.fetchone.return_value = (True,)
        with patch('app.cloud_worker.psycopg.connect', return_value=connection):
            with polling_lease('postgresql://localhost/example'):
                pass
        self.assertEqual(connection.execute.call_count, 2)
        connection.close.assert_called_once()

        connection = unittest.mock.Mock()
        connection.execute.return_value.fetchone.return_value = (False,)
        with patch('app.cloud_worker.psycopg.connect', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, 'already owns'):
                with polling_lease('postgresql://localhost/example'):
                    pass
        connection.close.assert_called_once()

    def test_fail_closed_config(self):
        values = dict(student_os_bridge_enabled=True, outbox_database_url='postgresql://example',
                      student_os_api_url='https://core.example', student_os_bridge_secret='x'*48)
        validate(SimpleNamespace(**values))
        for key, value in [('student_os_bridge_enabled',False), ('outbox_database_url','local.db'),
                           ('student_os_api_url','http://core.example'), ('student_os_bridge_secret','weak')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                validate(SimpleNamespace(**{**values,key:value}))
