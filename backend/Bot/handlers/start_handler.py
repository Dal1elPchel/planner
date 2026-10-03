from aiogram import Router, F, types

router = Router()

@router.message()
async def echo(message: types.Message):
    await message.answer(message.text)