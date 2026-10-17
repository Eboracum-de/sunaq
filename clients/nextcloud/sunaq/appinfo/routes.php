<?php
return [
    'routes' => [
        ['name' => 'page#index', 'url' => '/', 'verb' => 'GET'],
        ['name' => 'chat#send', 'url' => '/chat', 'verb' => 'POST'],
        ['name' => 'chat#stream', 'url' => '/chat/stream', 'verb' => 'POST'],
        ['name' => 'chat#models', 'url' => '/models', 'verb' => 'GET'],
        ['name' => 'chat#status', 'url' => '/status/{requestId}', 'verb' => 'GET'],
        ['name' => 'chat#listChats', 'url' => '/chats', 'verb' => 'GET'],
        ['name' => 'chat#load', 'url' => '/chats/{id}', 'verb' => 'GET'],
        ['name' => 'chat#rename', 'url' => '/chats/{id}/rename', 'verb' => 'POST'],
        ['name' => 'chat#delete', 'url' => '/chats/{id}', 'verb' => 'DELETE'],
        ['name' => 'fileAction#suggestName', 'url' => '/files/suggest-name', 'verb' => 'POST'],
        ['name' => 'fileAction#rename', 'url' => '/files/rename', 'verb' => 'POST'],
        ['name' => 'admin#save', 'url' => '/admin/config', 'verb' => 'POST'],
    ],
];
